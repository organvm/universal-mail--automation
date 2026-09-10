"""Complete reviewed correspondence to executable, account-bound flag plans.

The bundle is private evidence, never mutation authority. V6 plans retain the
existing approval, activation, transaction and override gates. Their additional
write preflight reads the current review and native binding sources again.
Historical plans remain readable for audit and guarded rollback.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.flag_policy import Classification, proposal_from_classification
from core.flag_workflow import (
    EVIDENCE_PLAN_SCHEMA, FlagWorkflowError, _json_object_from_path,
    _parse_aware_timestamp, _require_exact_keys, require_hex256, sha256_hex,
)
from core.mail_inventory import seal, validate
from core.models import MessageReference
from core.obligation_workflow import MessageIdentity, Obligation, POLICY_HASH, reconcile

SCHEMA = "uma.flags.reviewed_evidence.v1"
POSTURE_ORDER = ("REVIEW", "NOW", "ACTION", "WAITING", "SCHEDULED", "REFERENCE", "LATER")


def _age(value, now, label):
    stamp = _parse_aware_timestamp(value, label)
    if not timedelta(0) <= now - stamp <= timedelta(hours=24):
        raise FlagWorkflowError(f"{label} is stale or in the future")


def _artifact(source, label, *, read):
    _require_exact_keys(source, {"path", "sha256", "artifact"}, label)
    path = Path(source["path"])
    if not path.is_absolute():
        raise FlagWorkflowError(f"{label} requires an absolute private source path")
    require_hex256(source["sha256"], f"{label}.sha256")
    artifact = source["artifact"]
    validate(artifact)
    if artifact["content_hash"] != source["sha256"]:
        raise FlagWorkflowError(f"{label} embedded source hash mismatch")
    if read:
        current = _json_object_from_path(path, label)
        validate(current)
        if current != artifact:
            raise FlagWorkflowError(f"{label} changed after planning; research and approval must be renewed")
    return artifact


def _source(path, label):
    path = Path(path).expanduser().resolve()
    artifact = _json_object_from_path(path, label)
    validate(artifact)
    return {"path": str(path), "sha256": artifact["content_hash"], "artifact": artifact}


def build_evidence_bundle(*, corpus_path, research_root, reviews_root,
                          binding_paths, now=None):
    """Pack all independent obligations in every selected complete thread.

    Selection is by exact native binding. There is no obligation subset input:
    every obligation in each selected thread's current review is retained.
    Provider/source validation is delegated to the native binding contract.
    """
    from core.corpus_review import thread_evidence
    from core.native_mailapp_binding import validate_binding

    now = now or datetime.now(timezone.utc)
    corpus = _json_object_from_path(Path(corpus_path), "research corpus")
    validate(corpus)
    bindings = [_source(path, "native Mail.app binding") for path in binding_paths]
    if not bindings:
        raise FlagWorkflowError("explicit native bindings are required")
    selected = set()
    for source in bindings:
        validate_binding(source["artifact"], now=now, revalidate_sources=True)
        selected.add(MessageIdentity.model_validate(source["artifact"]["identity"]).key)
    threads, reviewed, covered = [], {}, set()
    for thread in corpus["threads"]:
        identities = {MessageIdentity.model_validate(p["identity"]).key
                      for message in thread["messages"] for p in message["provenance"]}
        if not selected & identities:
            continue
        _, evidence = thread_evidence(corpus, Path(research_root), thread["id"])
        source = _source(Path(reviews_root) / (thread["id"] + ".json"), "current correspondence review")
        review = source["artifact"]
        for obligation in review["obligations"]:
            oid = obligation["id"]
            if oid in reviewed:
                raise FlagWorkflowError("duplicate independent obligation id across selected threads")
            reviewed[oid] = sha256_hex(obligation)
        threads.append({"thread_id": thread["id"], "review_source": source,
                        "messages": [{"message_id": message["id"],
                                      "identities": [p["identity"] for p in message["provenance"]],
                                      "receipt_sha256": row["content_hash"],
                                      "attachments": [a["sha256"] for a in row["attachments"]]}
                                     for message, row in zip(thread["messages"], evidence)]})
        covered.update(identities)
    if not selected <= covered:
        raise FlagWorkflowError("binding has no complete reviewed correspondence thread")
    result = seal({"schema": SCHEMA, "generated_at": now.isoformat(),
                   "obligation_policy_sha256": POLICY_HASH,
                   "corpus_sha256": corpus["content_hash"],
                   "inventory_receipts": [s["inventory_sha256"] for s in corpus["sources"]],
                   "bindings": bindings, "threads": threads,
                   "reviewed_obligations": reviewed, "authority": "evidence_only"})
    validate_evidence_bundle(result, now=now)
    return result


def validate_evidence_bundle(bundle, *, now, revalidate_sources=False):
    """Strictly validate coverage, complete reviews and native account lineage."""
    from core.native_mailapp_binding import validate_binding

    try:
        validate(bundle)
        _require_exact_keys(bundle, {"schema", "generated_at", "obligation_policy_sha256",
                                    "corpus_sha256", "inventory_receipts", "bindings", "threads",
                                    "reviewed_obligations", "authority", "content_hash"}, "flag evidence")
        if bundle["schema"] != SCHEMA or bundle["authority"] != "evidence_only":
            raise FlagWorkflowError("versioned reviewed flag evidence required")
        if bundle["obligation_policy_sha256"] != POLICY_HASH:
            raise FlagWorkflowError("obligation evidence policy changed")
        _age(bundle["generated_at"], now, "flag evidence")
        require_hex256(bundle["corpus_sha256"], "evidence corpus hash")
        if not bundle["inventory_receipts"]:
            raise FlagWorkflowError("native inventory coverage is required")
        for digest in bundle["inventory_receipts"]:
            require_hex256(digest, "inventory receipt hash")
        bindings, native_keys = {}, set()
        for source in bundle["bindings"]:
            binding = _artifact(source, "native Mail.app binding", read=revalidate_sources)
            ref = validate_binding(binding, now=now, revalidate_sources=revalidate_sources)
            identity = MessageIdentity.model_validate(binding["identity"])
            if ref.ref_digest in bindings or identity.key in native_keys:
                raise FlagWorkflowError("duplicate native identity or Mail.app flag target")
            bindings[ref.ref_digest] = {"reference": ref, "identity": identity, "binding": binding}
            native_keys.add(identity.key)
        if not bindings:
            raise FlagWorkflowError("evidence requires explicit native bindings")
        obligations, reviewed, covered, thread_ids = [], {}, set(), set()
        for thread in bundle["threads"]:
            _require_exact_keys(thread, {"thread_id", "review_source", "messages"}, "review thread")
            if thread["thread_id"] in thread_ids:
                raise FlagWorkflowError("duplicate review thread")
            thread_ids.add(thread["thread_id"])
            review = _artifact(thread["review_source"], "current correspondence review", read=revalidate_sources)
            if review.get("schema") != "uma.correspondence_review.v1" or review.get("authority") != "review_only" \
                    or review.get("writes_performed") != 0:
                raise FlagWorkflowError("completed correspondence review required")
            _age(review["reviewed_at"], now, "correspondence review")
            if review["corpus_sha256"] != bundle["corpus_sha256"] or review["thread_id"] != thread["thread_id"]:
                raise FlagWorkflowError("review corpus or thread lineage mismatch")
            receipts, identities, attachments = {}, set(), set()
            message_ids = set()
            for message in thread["messages"]:
                _require_exact_keys(message, {"message_id", "identities", "receipt_sha256", "attachments"}, "review message")
                digest = require_hex256(message["receipt_sha256"], "message receipt hash")
                keys = {MessageIdentity.model_validate(i).key for i in message["identities"]}
                if not keys or digest in receipts or message["message_id"] in message_ids:
                    raise FlagWorkflowError("missing or duplicated complete message coverage")
                message_ids.add(message["message_id"])
                receipts[digest] = keys
                identities.update(keys)
                attachments.update((digest, require_hex256(a, "attachment hash")) for a in message["attachments"])
            if review["message_receipts"] != list(receipts):
                raise FlagWorkflowError("review must retain every message receipt in chronology order")
            if not identities or not identities & native_keys:
                raise FlagWorkflowError("review thread has no selected native identity")
            attachment_reviews = review["attachment_reviews"]
            if {(a["message_receipt"], a["sha256"]) for a in attachment_reviews} != attachments:
                raise FlagWorkflowError("attachment coverage omitted from review")
            for attachment in attachment_reviews:
                if attachment["disposition"] not in ("relevant_read", "not_relevant", "question") or not attachment.get("reason"):
                    raise FlagWorkflowError("attachment relevance must be reviewed")
                if attachment["disposition"] == "relevant_read" and not attachment.get("evidence_ref"):
                    raise FlagWorkflowError("relevant attachment has no read evidence")
            thread_obligations = [Obligation.model_validate(o) for o in review["obligations"]]
            if any(a["disposition"] == "question" for a in attachment_reviews) and not any(o.questions for o in thread_obligations):
                raise FlagWorkflowError("unanswered attachment question was removed")
            obligation_keys = set()
            for obligation in thread_obligations:
                if obligation.id in reviewed or obligation.thread_id != thread["thread_id"]:
                    raise FlagWorkflowError("duplicate or incorrectly scoped independent obligation")
                keys = {m.key for m in obligation.messages}
                if not keys <= identities:
                    raise FlagWorkflowError("obligation references an uncollected native identity")
                for event in obligation.evidence:
                    if event.source == "correspondence" and event.message.key not in receipts.get(event.proof.removeprefix("sha256:"), set()):
                        raise FlagWorkflowError("correspondence proof does not bind the exact message receipt")
                reviewed[obligation.id] = sha256_hex(obligation.model_dump(mode="json"))
                obligation_keys.update(keys)
            if obligation_keys != identities:
                raise FlagWorkflowError("review omitted a retained message identity")
            obligations.extend(thread_obligations)
            covered.update(identities)
        if not native_keys <= covered or reviewed != bundle["reviewed_obligations"]:
            raise FlagWorkflowError("incomplete independent obligation coverage")
        return bindings, reconcile(obligations, now=now)["obligations"]
    except (ValueError, TypeError, KeyError, OSError) as exc:
        if isinstance(exc, FlagWorkflowError):
            raise
        raise FlagWorkflowError(f"invalid reviewed flag evidence: {exc}") from exc


def _classification(posture, bundle, siblings):
    semantic, actor, state = {
        "NOW": ("action_request", "operator", "open_loop"),
        "ACTION": ("action_request", "operator", "open_loop"),
        "WAITING": ("awaiting_other_party", "other_party", "open_loop"),
        "SCHEDULED": ("event_confirmed", "none", "open_loop"),
        "REFERENCE": ("active_reference", "none", "reference_only"),
        "REVIEW": ("unidentifiable_action", "unknown", "open_loop"),
        "LATER": ("deferred_item", "none", "deferred"),
        None: ("closed_or_receipt", "none", "closed"),
    }[posture]
    return Classification(domain="general", semantic_type=semantic,
                          urgency="imminent" if posture == "NOW" else "none",
                          next_action_owner=actor, operator_state=state,
                          due_evidence="reviewed deadline" if posture == "NOW" else None,
                          follow_up_evidence="reviewed correspondence" if posture == "WAITING" else None,
                          marketing_or_bulk=False, confidence=1.0,
                          evidence_basis=("reviewed_bundle:" + bundle["content_hash"],
                                          "obligations:" + sha256_hex(sorted(r["id"] for r in siblings))))


def _decisions(bundle, *, now, revalidate_sources=False):
    bindings, rows = validate_evidence_bundle(bundle, now=now, revalidate_sources=revalidate_sources)
    for item in bindings.values():
        key = item["identity"].key
        siblings = [r for r in rows if any(MessageIdentity.model_validate(m).key == key for m in r["messages"])]
        if not siblings:
            raise FlagWorkflowError("selected identity has no researched obligation")
        blockers = [reason for reason in ("protected", "human_override") if any(r[reason] for r in siblings)]
        postures = {r["posture"] for r in siblings if r["posture"]}
        posture = next((p for p in POSTURE_ORDER if p in postures), None)
        item.update(classification=_classification(posture, bundle, siblings),
                    blockers=blockers, obligation_ids=[r["id"] for r in siblings])
    return bindings


def decisions_for_snapshot(snapshot, bundle, generated_at):
    now = _parse_aware_timestamp(generated_at, "evidence plan generated_at")
    decisions = _decisions(bundle, now=now)
    snapshot_refs = {m.ref_digest: m for m in snapshot.messages}
    if not decisions.keys() <= snapshot_refs.keys():
        raise FlagWorkflowError("native binding is absent from the exact Mail.app snapshot scope")
    for digest, item in decisions.items():
        m, ref = snapshot_refs[digest], item["reference"]
        expected = MessageReference.compute_evidence_digest(m.received_iso, m.sender, m.subject)
        if ref.evidence_digest != expected or ref.observed_native_flag != m.native_index:
            raise FlagWorkflowError("native binding and snapshot evidence or observed flag differ")
        require_hex256(ref.message_id_digest, "native binding RFC Message-ID digest")
        item["proposal"] = None if item["blockers"] else proposal_from_classification(item["classification"], m.observed_flag)
    return decisions


def validate_evidence_plan(plan, mutations, *, now=None, revalidate_sources=False):
    """Validate v6 decisions without inventing a new approval mechanism."""
    if plan.get("schema") != EVIDENCE_PLAN_SCHEMA:
        return
    from core.flag_workflow import ClassificationProof
    from providers.mailapp import flag_from_mailapp_index

    now = now or _parse_aware_timestamp(plan["generated_at"], "plan generation")
    decisions = _decisions(plan["evidence"], now=now, revalidate_sources=revalidate_sources)
    expected = {}
    for digest, item in decisions.items():
        ref = item["reference"]
        proposal = proposal_from_classification(item["classification"], flag_from_mailapp_index(ref.observed_native_flag))
        if not item["blockers"] and not proposal.is_identity:
            expected[digest] = (item, proposal)
    if {m.ref_digest for m in mutations} != set(expected):
        raise FlagWorkflowError("evidence plan omitted a required mutation or changed a protected identity")
    for mutation in mutations:
        item, proposal = expected[mutation.ref_digest]
        ref = item["reference"]
        proof = ClassificationProof.create(item["classification"], policy_sha256=plan["policy_sha256"],
                                           snapshot_sha256=plan["snapshot_sha256"], ref_digest=mutation.ref_digest,
                                           evidence_digest=ref.evidence_digest)
        if (mutation.proposed_flag != proposal.proposed_flag or mutation.classification_proof != proof
                or mutation.message_id_digest != ref.message_id_digest or mutation.evidence_digest != ref.evidence_digest
                or mutation.observed_native_flag != ref.observed_native_flag):
            raise FlagWorkflowError("mutation does not match complete reviewed evidence and native identity")


def validate_live_evidence(plan):
    """Read current sources at each write boundary, including a fresh canary."""
    if plan.get("schema") != EVIDENCE_PLAN_SCHEMA:
        return
    from core.flag_workflow import validate_plan_schema
    mutations = validate_plan_schema(plan)
    validate_evidence_plan(plan, mutations, now=datetime.now(timezone.utc), revalidate_sources=True)


def cmd_bundle(args):
    from core.flag_workflow import _atomic_write_private_json
    try:
        bundle = build_evidence_bundle(corpus_path=Path(args.corpus).expanduser(),
                                       research_root=Path(args.research).expanduser(),
                                       reviews_root=Path(args.reviews).expanduser(),
                                       binding_paths=args.binding)
        _atomic_write_private_json(Path(args.output).expanduser(), bundle, prefix=".tmp-flag-evidence-")
    except (ValueError, OSError) as exc:
        print(f"flags evidence: {exc}")
        return 20
    print(f"Flag evidence prepared: {len(bundle['bindings'])} exact identities; "
          f"{len(bundle['reviewed_obligations'])} independent obligations; zero writes")
    return 0
