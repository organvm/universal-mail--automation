"""Evidence-derived obligations. No email text, classifier, or receipt grants authority.

This additive contract keeps legacy sender-group ledgers readable without
pretending their aggregate keys identify individual obligations.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.flag_workflow import sha256_hex

SCHEMA = "uma.obligations.v2"
POLICY = {"version": "obligation-evidence.2", "fresh_hours": 24,
          "immediate_hours": 24, "max_threads": 25, "messages_per_thread": 20,
          "max_mutations": 25, "run_seconds": 600, "identity_refreshes": 1}
POLICY_HASH = sha256_hex(POLICY)
POSTURES = {"NOW": "red", "ACTION": "orange", "WAITING": "yellow",
            "SCHEDULED": "green", "REFERENCE": "blue", "REVIEW": "purple",
            "LATER": "gray"}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MessageIdentity(Contract):
    provider: Literal["gmail", "icloud", "mailapp", "outlook", "imap"]
    account: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    # Account-scoped stable provider id, or RFC Message-ID plus envelope digest.
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def key(self) -> str:
        return sha256_hex(self.model_dump())


def aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


class Evidence(Contract):
    id: str = Field(min_length=1)
    obligation_id: str = Field(min_length=1)
    message: MessageIdentity
    occurred_at: datetime
    observed_at: datetime
    source: Literal["correspondence", "provider", "operator", "legacy"]
    fact: Literal["request", "waiting", "commitment", "reference", "deferred",
                  "completed", "reopened", "non_action", "unknown"]
    next_actor: Literal["operator", "other", "none", "unknown"] = "unknown"
    next_action: str = ""
    deadline: datetime | None = None
    consequential: bool = False
    # A resolver receipt or exact correspondence excerpt reference; never a draft.
    proof: str = ""

    @model_validator(mode="after")
    def validate_times(self):
        if aware(self.occurred_at) > aware(self.observed_at):
            raise ValueError("evidence occurrence cannot follow its observation")
        if self.deadline is not None:
            aware(self.deadline)
        if self.source == "legacy" and self.fact != "unknown":
            raise ValueError("legacy aggregates cannot establish obligation state")
        if self.fact == "deferred" and self.source != "operator":
            raise ValueError("deliberate deferral requires an operator decision")
        return self


class Question(Contract):
    reason: Literal["missing_evidence", "chronology", "external_status", "personal_choice",
                    "conflicting_evidence", "stale_evidence", "coverage_gap"]
    question: str = Field(min_length=1)
    resolver: str = Field(min_length=1)
    checkpoint: datetime

    @model_validator(mode="after")
    def validate_time(self):
        aware(self.checkpoint)
        return self


class Obligation(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    messages: tuple[MessageIdentity, ...] = Field(min_length=1)
    evidence: tuple[Evidence, ...] = ()
    questions: tuple[Question, ...] = ()
    protected: bool = False
    human_override: bool = False

    @model_validator(mode="after")
    def validate_binding(self):
        keys = [m.key for m in self.messages]
        identities = [(m.provider, m.account, m.message_id) for m in self.messages]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate message identity")
        if len({(m.provider, m.account) for m in self.messages}) != 1:
            raise ValueError("an obligation must have one account scope")
        if len({e.id for e in self.evidence}) != len(self.evidence):
            raise ValueError("duplicate evidence id")
        for e in self.evidence:
            if e.obligation_id != self.id or e.message.key not in keys:
                raise ValueError("evidence belongs to a different obligation or message")
        return self


def derive(ob: Obligation, *, now: datetime) -> dict:
    """Derive chronology for one obligation, never the subject or whole thread."""
    now = aware(now)
    questions = [q.model_dump(mode="json") for q in ob.questions]

    def review(reason: str, question: str, resolver: str):
        questions.append({"reason": reason, "question": question, "resolver": resolver,
                          "checkpoint": (now + timedelta(days=1)).isoformat()})

    evidence = sorted(ob.evidence, key=lambda e: (e.occurred_at, e.id))
    valid = [e for e in evidence if e.source != "legacy" and e.proof
             and e.observed_at <= now and e.occurred_at <= now]
    latest = valid[-1] if valid else None
    posture, lifecycle, actor, action = "REVIEW", "open", "unknown", "Resolve the evidence question"
    if latest is None:
        review("missing_evidence", "What exact action remains for this obligation?",
               "bounded_thread_and_sent_read")
    else:
        actor, action = latest.next_actor, latest.next_action
        if (now - latest.observed_at).total_seconds() > POLICY["fresh_hours"] * 3600:
            review("stale_evidence", "Is this obligation still in the observed state?",
                   "bounded_thread_and_sent_read")
        same_time = {e.fact for e in valid if e.occurred_at == latest.occurred_at}
        if len(same_time) > 1:
            review("conflicting_evidence", "Which simultaneous assertion is authoritative?",
                   "chronology_resolver")
        if latest.fact == "completed" and latest.source in ("provider", "operator", "correspondence"):
            lifecycle, posture = "completed", None
        elif latest.fact == "non_action":
            lifecycle, posture = "non_action", None
        elif latest.fact in ("request", "reopened") and actor == "operator" and action:
            posture = "ACTION"
            if latest.consequential and latest.deadline is not None and latest.deadline <= now + timedelta(hours=24):
                posture = "NOW"
        elif latest.fact == "waiting" and latest.source == "correspondence" and actor == "other" and action:
            posture = "WAITING"
        elif latest.fact == "commitment" and latest.deadline is not None and latest.deadline > now:
            posture = "SCHEDULED"
        elif latest.fact == "reference":
            posture = "REFERENCE"
        elif latest.fact == "deferred" and latest.deadline is not None and latest.deadline > now:
            posture = "LATER"
        else:
            review("chronology", "What happened after this request or past commitment?",
                   "chronology_resolver")
    if questions:
        posture, lifecycle = "REVIEW", "open"
    return {"id": ob.id, "title": ob.title, "thread_id": ob.thread_id,
            "messages": [m.model_dump() for m in ob.messages],
            "posture": posture, "lifecycle": lifecycle, "next_actor": actor,
            "next_action": action, "deadline": latest.deadline.isoformat() if latest and latest.deadline else None,
            "evidence_ids": [e.id for e in valid], "questions": questions,
            "evidence_observed_at": latest.observed_at.isoformat() if latest else None,
            "review_age_hours": max(0, (now - min(e.observed_at for e in evidence)).total_seconds() / 3600) if evidence and questions else 0,
            "protected": ob.protected, "human_override": ob.human_override,
            "verification_status": "not_requested"}


def reconcile(obligations: list[Obligation], *, now: datetime,
              coverage_gaps: list[str] | None = None,
              verification_receipts: list[dict] | None = None) -> dict:
    now = aware(now)
    if len({ob.id for ob in obligations}) != len(obligations):
        raise ValueError("duplicate obligation id")
    rows = [derive(ob, now=now) for ob in obligations]
    verifications = {}
    for receipt in verification_receipts or []:
        body = {k: v for k, v in receipt.items() if k != "content_hash"}
        if receipt.get("schema") != "uma.flags.verification.v1" or sha256_hex(body) != receipt.get("content_hash"):
            raise ValueError("invalid verification receipt")
        checked = aware(datetime.fromisoformat(receipt["observed_at"]))
        if checked > now or now - checked > timedelta(hours=24):
            continue
        for item in receipt["results"]:
            ref = item["reference"]
            key = (ref["provider"], ref["account"], ref["provider_id"], ref["evidence_digest"])
            if key not in verifications or checked > verifications[key][0]:
                verifications[key] = (checked, item["status"])
    # Any active sibling prevents archiving shared message evidence.
    active = {m["account"] + ":" + m["provider"] + ":" + m["message_id"]
              for row in rows if row["lifecycle"] == "open"
              for m in row["messages"]}
    for row in rows:
        states = [verifications.get((m["provider"], m["account"], m["message_id"], m["evidence_digest"]))
                  for m in row["messages"]]
        statuses = [v[1] if v else "not_requested" for v in states]
        row["verification_status"] = next((s for s in ("conflicted", "uncertain", "unchanged", "not_requested")
                                            if s in statuses), "verified")
        row["archive_eligible"] = (
            row["lifecycle"] in ("non_action", "completed") and not row["protected"]
            and not row["human_override"] and not coverage_gaps
            and not any(m["account"] + ":" + m["provider"] + ":" + m["message_id"] in active
                        for m in row["messages"]))
    result = {"schema": SCHEMA, "generated_at": aware(now).isoformat(),
              "policy_version": POLICY["version"], "policy_sha256": POLICY_HASH,
              "obligations": rows, "coverage_gaps": coverage_gaps or [],
              "metrics": {"total": len(rows), "unresolved": sum(r["lifecycle"] == "open" for r in rows),
                          "review": sum(r["posture"] == "REVIEW" for r in rows),
                          "coverage_gaps": len(coverage_gaps or []),
                          "conflicts": sum(r["verification_status"] == "conflicted" for r in rows),
                          "verification_failures": sum(r["verification_status"] == "uncertain" for r in rows),
                          "incorrect_archives": None,
                          "max_review_age_hours": max((r["review_age_hours"] for r in rows), default=0),
                          "by_posture": dict(Counter(r["posture"] or "CLOSED" for r in rows))},
              "authority": "shadow_only"}
    result["content_hash"] = sha256_hex(result)
    return result


def import_legacy(ledger: dict, *, now: datetime) -> list[Obligation]:
    """Explicit adapter: preserve every aggregate as REVIEW, never infer stars."""
    result = []
    for index, row in enumerate(ledger.get("obligations", [])):
        for account in row.get("accounts") or ["unknown"]:
            oid = "legacy-" + sha256_hex({"index": index, "account": account, "row": row})
            result.append(Obligation(
                id=oid, title=row.get("title") or "Legacy obligation requires evidence",
                thread_id=oid,
                messages=(MessageIdentity(provider="mailapp", account=account,
                                          message_id=oid, evidence_digest=sha256_hex(row)),),
                questions=(Question(reason="coverage_gap",
                                    question="Which exact messages and distinct obligations does this aggregate represent?",
                                    resolver="bounded_thread_and_sent_read", checkpoint=now + timedelta(days=1)),)))
    return result


def verify_archive(*, provider: str, expected: MessageIdentity, observed: dict,
                   destination: str | None = None) -> str:
    """Provider proof adapter. Absence from a local Inbox alone is never proof."""
    if provider != expected.provider or observed.get("identity") != expected.model_dump():
        return "conflicted"
    if observed.get("server_confirmed") is not True:
        return "uncertain"
    if provider == "gmail":
        labels = observed.get("label_ids")
        if not isinstance(labels, list) or any(not isinstance(v, str) for v in labels):
            return "uncertain"
        if "TRASH" in labels or "SPAM" in labels:
            return "conflicted"
        if observed.get("in_inbox") is True:
            return "unchanged"
        return "verified" if "INBOX" not in labels else "unchanged"
    if provider in ("icloud", "outlook"):
        members = observed.get("mailboxes")
        if not destination or not isinstance(members, list):
            return "uncertain"
        if observed.get("in_inbox") is not False:
            return "uncertain"
        if any(str(m).casefold() == "inbox" for m in members):
            return "unchanged"
        return "verified" if destination in members else "uncertain"
    return "unsupported"


def evaluate_benchmark(cases: list[dict], *, now: datetime) -> dict:
    """Reviewed, redacted cases only. Results propose policy; never promote it."""
    results = []
    for case in cases:
        if case.get("reviewed") is not True:
            raise ValueError("benchmark corrections must be reviewed")
        ob = Obligation.model_validate(case["obligation"])
        as_of = aware(datetime.fromisoformat(case["as_of"]))
        actual = derive(ob, now=as_of)["posture"]
        results.append({"case_id": case["id"], "passed": actual == case["expected_posture"]})
    return {"schema": "uma.obligation_benchmark.v1", "policy_sha256": POLICY_HASH,
            "benchmark_sha256": sha256_hex(cases), "results": results,
            "passed": all(r["passed"] for r in results), "promotion": "review_required"}


def flag_candidates(view: dict, flag_plan: dict) -> dict:
    """Explicit bridge to preserved mutation ids, without rewriting their authority.

    Multiple obligations stay independent. REVIEW remains visible on a shared
    message whenever one sibling still has unresolved evidence.
    """
    from core.flag_workflow import validate_plan_schema
    body = {k: v for k, v in view.items() if k != "content_hash"}
    if view.get("schema") != SCHEMA or sha256_hex(body) != view.get("content_hash"):
        raise ValueError("invalid obligation view")
    if view.get("policy_sha256") != POLICY_HASH:
        raise ValueError("obligation policy mismatch")
    mutations = validate_plan_schema(flag_plan)
    grouped = {}
    for row in view["obligations"]:
        for msg in row["messages"]:
            key = (msg["provider"], msg["account"], msg["message_id"], msg["evidence_digest"])
            grouped.setdefault(key, []).append(row)
    result = []
    for key, siblings in grouped.items():
        matches = [m for m in mutations if (m.provider, m.account, m.provider_id, m.evidence_digest) == key]
        postures = {r["posture"] for r in siblings if r["posture"]}
        posture = next((p for p in ("REVIEW", "NOW", "ACTION", "WAITING", "SCHEDULED", "REFERENCE", "LATER") if p in postures), None)
        target = POSTURES.get(posture, "no_flag")
        blockers = []
        if view["coverage_gaps"]:
            blockers.append("coverage_incomplete")
        if len(matches) != 1:
            blockers.append("exact_native_plan_identity_unavailable")
        if any(r["human_override"] for r in siblings):
            blockers.append("human_override")
        if posture == "REVIEW":
            blockers.append("obligation_review_required")
        result.append({"obligation_ids": [r["id"] for r in siblings],
                       "mutation_id": matches[0].mutation_id if len(matches) == 1 else None,
                       "target_flag": target, "blockers": blockers,
                       "selection_status": "blocked" if blockers else "requires_explicit_canary_selection"})
    return {"schema": "uma.obligation_flag_candidates.v1", "obligations_sha256": view["content_hash"],
            "flag_plan_sha256": flag_plan["plan_hash"], "candidates": result,
            "authority": "none", "writes_performed": 0}
