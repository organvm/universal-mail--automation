"""Reviewed obligations bound to complete correspondence and attachment evidence.

Collection, semantic review, unresolved work, and mutation approval are separate
states. This module records review; it never creates an approval or sends mail.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from core.flag_transactions import AdvisoryFileLock
from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex
from core.mail_inventory import seal, validate
from core.obligation_workflow import Obligation, MessageIdentity, reconcile


class ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "div", "br", "tr", "li"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def readable_body(evidence):
    plain = [b["text"] for b in evidence["bodies"] if b["content_type"] == "text/plain"]
    if plain:
        return "\n".join(plain)
    parser = ReadableHTML()
    for body in evidence["bodies"]:
        parser.feed(body["text"])
    return "\n".join(line.strip() for line in "".join(parser.text).splitlines() if line.strip())


def thread_evidence(corpus, research_root, thread_id):
    validate(corpus)
    state = _json_object_from_path(research_root / "manifest.json", "research manifest")
    validate(state)
    if state.get("corpus_sha256") != corpus["content_hash"]:
        raise ValueError("review research lineage mismatch")
    matches = [t for t in corpus["threads"] if t["id"] == thread_id]
    progress = [t for t in state["threads"] if t["id"] == thread_id]
    if len(matches) != 1 or len(progress) != 1:
        raise ValueError("review thread identity mismatch")
    thread, progress = matches[0], progress[0]
    if progress["next_message"] != len(thread["messages"]) or len(progress["receipts"]) != len(thread["messages"]):
        raise ValueError("complete thread collection required before review")
    evidence = []
    for message, receipt in zip(thread["messages"], progress["receipts"]):
        if receipt["file"] != "messages/" + receipt["sha256"] + ".json":
            raise ValueError("review receipt path mismatch")
        row = _json_object_from_path(research_root / receipt["file"], "message evidence")
        validate(row)
        if row["content_hash"] != receipt["sha256"] or row["message_id"] != message["id"] or row["provenance"] != message["provenance"]:
            raise ValueError("review message lineage mismatch")
        evidence.append(row)
    return thread, evidence


def record_review(corpus, *, research_root: Path, root: Path, review):
    if review.get("schema") != "uma.correspondence_review.v1" or review.get("corpus_sha256") != corpus["content_hash"]:
        raise ValueError("versioned corpus-bound review required")
    thread, evidence = thread_evidence(corpus, research_root, review["thread_id"])
    receipts = [e["content_hash"] for e in evidence]
    proof_identities = {"sha256:" + e["content_hash"]:
                        {MessageIdentity.model_validate(p["identity"]).key for p in e["provenance"]}
                        for e in evidence}
    if review["message_receipts"] != receipts:
        raise ValueError("review must cover all messages in chronology order")
    identities = {MessageIdentity.model_validate(p["identity"]).key
                  for message in thread["messages"] for p in message["provenance"]}
    obligations = [Obligation.model_validate(o) for o in review["obligations"]]
    if not obligations or len({o.id for o in obligations}) != len(obligations):
        raise ValueError("review requires independent uniquely identified obligations or non-action dispositions")
    covered = set()
    for obligation in obligations:
        if obligation.thread_id != thread["id"] or any(m.key not in identities for m in obligation.messages):
            raise ValueError("review obligation identity mismatch")
        for event in obligation.evidence:
            if event.source == "correspondence" and event.message.key not in proof_identities.get(event.proof, set()):
                raise ValueError("correspondence proof must bind a collected message receipt")
        covered.update(m.key for m in obligation.messages)
    if covered != identities:
        raise ValueError("review omitted a message or duplicate membership identity")
    attachments = {(e["content_hash"], a["sha256"]) for e in evidence for a in e["attachments"]}
    reviews = review["attachment_reviews"]
    if {(a["message_receipt"], a["sha256"]) for a in reviews} != attachments:
        raise ValueError("review omitted attachment relevance decisions")
    for attachment in reviews:
        if attachment["disposition"] not in ("relevant_read", "not_relevant", "question") or not attachment.get("reason"):
            raise ValueError("attachment review needs a disposition and reason")
        if attachment["disposition"] == "relevant_read" and not attachment.get("evidence_ref"):
            raise ValueError("relevant attachment requires a read evidence reference")
        if attachment["disposition"] == "question" and not any(o.questions for o in obligations):
            raise ValueError("unread relevant attachment must leave an obligation question")
    now = datetime.now(timezone.utc)
    view = reconcile(obligations, now=now)
    result = seal({**review, "reviewed_at": now.isoformat(), "authority": "review_only",
                   "obligation_view": view, "writes_performed": 0})
    with AdvisoryFileLock(root / "reviews.lock"):
        path = root / (thread["id"] + ".json")
        if path.exists():
            previous = _json_object_from_path(path, "previous review")
            validate(previous)
            if review.get("supersedes") != previous["content_hash"]:
                raise ValueError("updated review must explicitly supersede prior evidence")
            _atomic_write_private_json(root / "history" / (previous["content_hash"] + ".json"), previous, prefix=".tmp-review-history-")
        _atomic_write_private_json(path, result, prefix=".tmp-review-")
    return result


def archive_coverage(corpus, *, research_root, reviews_root, message_keys):
    validate(corpus)
    selected = set(message_keys)
    if not selected:
        raise ValueError("coverage requires explicit selected identities")
    covered, receipts, reviewed = set(), set(), {}
    for thread in corpus["threads"]:
        identities = {MessageIdentity.model_validate(p["identity"]).key
                      for message in thread["messages"] for p in message["provenance"]}
        if not selected & identities:
            continue
        _, evidence = thread_evidence(corpus, research_root, thread["id"])
        review = _json_object_from_path(reviews_root / (thread["id"] + ".json"), "correspondence review")
        validate(review)
        if review["corpus_sha256"] != corpus["content_hash"] or review["message_receipts"] != [e["content_hash"] for e in evidence]:
            raise ValueError("coverage review lineage mismatch")
        if any(a["disposition"] == "question" for a in review["attachment_reviews"]):
            raise ValueError("unresolved attachment coverage")
        obligations = [Obligation.model_validate(o) for o in review["obligations"]]
        view = reconcile(obligations, now=datetime.now(timezone.utc))
        if any(row["questions"] for row in view["obligations"]):
            raise ValueError("unresolved correspondence coverage")
        for obligation in obligations:
            reviewed[obligation.id] = sha256_hex(obligation.model_dump(mode="json"))
        covered.update(identities)
        receipts.update(e["content_hash"] for e in evidence)
    if not selected <= covered:
        raise ValueError("selected archive identity has no complete reviewed thread")
    return seal({"schema": "uma.archive_coverage.v2", "complete": True, "questions": [],
                 "observed_at": datetime.now(timezone.utc).isoformat(), "corpus_sha256": corpus["content_hash"],
                 "inventory_receipts": [s["inventory_sha256"] for s in corpus["sources"]],
                 "research_receipts": sorted(receipts), "message_keys": sorted(covered),
                 "reviewed_obligations": reviewed})
