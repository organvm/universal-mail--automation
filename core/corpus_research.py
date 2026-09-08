"""Resumable RFC-linked research across retained Inbox, Sent, and archives.

The index groups correspondence, not obligations. Every message and membership is
retained; no subject heuristic or collected task establishes resolution.
"""
from datetime import datetime, timezone
import email
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
import re
import time

from core.flag_transactions import AdvisoryFileLock
from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex
from core.mail_inventory import iter_messages, seal, validate


def build_corpus(inventory_roots: list[Path], *, output: Path):
    messages, sources, parents = {}, [], {}

    def find(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def union(left, right):
        parents[find(right)] = find(left)

    for root in inventory_roots:
        manifest = _json_object_from_path(root / "manifest.json", "inventory manifest")
        validate(manifest)
        if not manifest["complete"]:
            raise ValueError("complete account inventory required for corpus construction")
        sources.append({"identity": manifest["identity"], "inventory_sha256": manifest["content_hash"]})
        for message in iter_messages(root):
            if message["retention_class"] != "retained":
                continue
            identity = message["identity"]
            scope = sha256_hex({"provider": identity["provider"], "account": identity["account"]})
            # Only native identity establishes duplicate memberships. Different
            # messages can share RFC headers while carrying different bodies.
            key = sha256_hex(identity)
            if key not in messages:
                # Full headers and server responses stay in their inventory
                # receipts rather than being duplicated into the working index.
                projected = {k: v for k, v in message.items() if k not in ("headers", "server_metadata")}
                messages[key] = {"id": key, "message": projected, "provenance": []}
            messages[key]["provenance"].append({"identity": identity, "native": message["native"],
                                                 "memberships": message["memberships"]})
            anchors = re.findall(r"<[^<>\s]+>", " ".join(message.get(k, "")
                                 for k in ("rfc_message_id", "in_reply_to", "references")))
            find(key)
            for anchor in anchors:
                union(key, scope + ":rfc:" + anchor)
            if message.get("thread_id"):
                union(key, scope + ":thread:" + message["thread_id"])
    groups = {}
    for key, message in messages.items():
        groups.setdefault(find(key), []).append(message)

    def chronology(item):
        value = item["message"].get("date", "")
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return ("", item["id"])
        return (date.isoformat() if date.tzinfo is None else date.astimezone(timezone.utc).isoformat(), item["id"])

    threads = []
    for items in groups.values():
        ordered = sorted(items, key=chronology)
        tid = sha256_hex(sorted(m["id"] for m in items))
        threads.append({"id": tid, "messages": ordered})

    def research_priority(thread):
        messages = [item["message"] for item in thread["messages"]]
        active = (any("\\Flagged" in message.get("native_flags", []) for message in messages) or
                  any(name.upper() == "INBOX" for item in thread["messages"]
                      for provenance in item["provenance"] for name in provenance["memberships"]))
        github = any("github.com" in message.get("sender", "").casefold() for message in messages)
        return (0 if active else 1 if github else 2, thread["id"])
    result = seal({"schema": "uma.research_corpus.v1", "sources": sources,
                   "threads": sorted(threads, key=research_priority),
                   "authority": "correspondence_only", "writes_performed": 0})
    _atomic_write_private_json(output, result, prefix=".tmp-corpus-")
    return result


def extract_evidence(raw: bytes) -> dict:
    parsed = email.message_from_bytes(raw, policy=policy.default)
    bodies, attachments = [], []
    for part in parsed.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_content_disposition() == "attachment" or part.get_filename():
            attachments.append({"filename": part.get_filename(), "content_type": part.get_content_type(),
                                "bytes": len(payload), "sha256": __import__("hashlib").sha256(payload).hexdigest(),
                                "review_status": "requires_relevance_review"})
        elif part.get_content_type() in ("text/plain", "text/html"):
            bodies.append({"content_type": part.get_content_type(),
                           "text": payload.decode(part.get_content_charset() or "utf-8", errors="replace")})
    return {"headers": [[name, value.encode("utf-8", errors="replace").decode("utf-8")]
                        for name, value in parsed.raw_items()], "bodies": bodies, "attachments": attachments}


def research_corpus(corpus: dict, provider, *, root: Path, thread_limit: int = 25,
                    message_limit: int = 20, ceiling: int = 600):
    validate(corpus)
    if corpus.get("schema") != "uma.research_corpus.v1":
        raise ValueError("invalid research corpus")
    if type(thread_limit) is not int or not 1 <= thread_limit <= 25 or type(message_limit) is not int or not 1 <= message_limit <= 20:
        raise ValueError("research unit exceeds 25 candidates or 20 reads per candidate")
    started = time.monotonic()
    if not 30 <= ceiling <= 600:
        raise ValueError("research ceiling requires 30–600 seconds")
    with AdvisoryFileLock(root / "research.lock"):
        path = root / "manifest.json"
        if path.exists():
            state = _json_object_from_path(path, "research continuation")
            validate(state)
            if state.get("schema") != "uma.corpus_research.v1" or state["corpus_sha256"] != corpus["content_hash"]:
                raise ValueError("research continuation lineage mismatch")
            for thread in state["threads"]:
                for receipt in thread["receipts"]:
                    evidence = _json_object_from_path(root / receipt["file"], "research evidence")
                    validate(evidence)
                    if evidence["content_hash"] != receipt["sha256"]:
                        raise ValueError("research evidence lineage mismatch")
        else:
            state = {"schema": "uma.corpus_research.v1", "corpus_sha256": corpus["content_hash"],
                     "threads": [{"id": t["id"], "next_message": 0, "receipts": [], "errors": []}
                                 for t in corpus["threads"]], "writes_performed": 0, "authority": "evidence_only"}

        def persist():
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            state["collected_threads"] = sum(s["next_message"] == len(t["messages"])
                                               for s, t in zip(state["threads"], corpus["threads"]))
            state["complete"] = state["collected_threads"] == len(corpus["threads"])
            _atomic_write_private_json(path, seal(state), prefix=".tmp-research-corpus-")

        persist()
        attempted = 0
        read_count = 0
        for target, thread in zip(state["threads"], corpus["threads"]):
            if target["id"] != thread["id"] or not 0 <= target["next_message"] <= len(thread["messages"]):
                raise ValueError("research cursor mismatch")
            if target["next_message"] == len(thread["messages"]):
                continue
            if attempted >= thread_limit or time.monotonic() - started >= ceiling - 30:
                break
            attempted += 1
            for _ in range(message_limit):
                if target["next_message"] == len(thread["messages"]) or time.monotonic() - started >= ceiling - 30:
                    break
                message = thread["messages"][target["next_message"]]
                try:
                    evidence = provider.read_corpus_message(message)
                except (RuntimeError, ValueError, OSError) as exc:
                    target["errors"].append({"message": message["id"], "type": type(exc).__name__,
                                              "at": datetime.now(timezone.utc).isoformat()})
                    persist()
                    break
                evidence.update(schema="uma.corpus_message_evidence.v1", message_id=message["id"],
                                provenance=message["provenance"], observed_at=datetime.now(timezone.utc).isoformat())
                seal(evidence)
                name = "messages/" + evidence["content_hash"] + ".json"
                _atomic_write_private_json(root / name, evidence, prefix=".tmp-research-message-")
                target["receipts"].append({"file": name, "sha256": evidence["content_hash"]})
                target["next_message"] += 1
                read_count += 1
                persist()
        state["last_unit_reads"] = read_count
        persist()
        return state
