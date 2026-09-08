"""Conditional archive transactions using the preserved private storage and locks.

Provider adapters must implement exact server observations and a conditional
archive/restore operation. Legacy archive(message_id)->bool is deliberately
not adapted: it cannot establish identity, concurrency, or destination proof.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

from core.flag_transactions import AdvisoryFileLock
from core.flag_workflow import _atomic_write_private_json, _json_object_from_path, sha256_hex
from core.obligation_workflow import MessageIdentity, Obligation, POLICY_HASH, reconcile, verify_archive


def build_plan(obligations: list[Obligation], observations: list[dict], *, now: datetime) -> dict:
    rows = reconcile(obligations, now=now)["obligations"]
    eligible = {sha256_hex(m)
                for r in rows if r["archive_eligible"] for m in r["messages"]}
    mutations = []
    for observed in observations:
        identity = MessageIdentity.model_validate(observed["identity"])
        if identity.provider not in ("gmail", "icloud"):
            raise ValueError("provider-specific server archive proof is unavailable")
        key = sha256_hex(identity.model_dump())
        if key not in eligible or observed.get("server_confirmed") is not True:
            raise ValueError("archive requires evidence-backed non-action disposition and server observation")
        if observed.get("in_inbox") is not True or observed.get("revision") in (None, ""):
            raise ValueError("archive requires current Inbox membership and revision")
        if observed.get("protected") is not False or observed.get("human_override") is not False:
            raise ValueError("protection and override checks must explicitly pass")
        mutation = {"identity": identity.model_dump(), "before": observed,
                    "destination": observed.get("archive_destination")}
        if identity.provider == "icloud" and not mutation["destination"]:
            raise ValueError("iCloud archive destination required")
        mutation["id"] = "archive-" + sha256_hex(mutation)
        mutations.append(mutation)
    if not 1 <= len(mutations) <= 25 or len({m["identity"]["account"] for m in mutations}) != 1:
        raise ValueError("archive plan must contain 1–25 messages in one account")
    if len({sha256_hex(m["identity"]) for m in mutations}) != len(mutations):
        raise ValueError("duplicate archive identity")
    plan = {"schema": "uma.archive_plan.v1", "policy_sha256": POLICY_HASH,
            "created_at": now.isoformat(), "mutations": mutations,
            "obligations": [o.model_dump(mode="json") for o in obligations]}
    plan["content_hash"] = sha256_hex(plan)
    return plan


def validate_plan(plan: dict, now: datetime) -> None:
    created = datetime.fromisoformat(plan["created_at"])
    if created.tzinfo is None or not timedelta(0) <= now - created <= timedelta(hours=24):
        raise ValueError("archive plan is stale or future-dated")
    rebuilt = build_plan([Obligation.model_validate(o) for o in plan["obligations"]],
                         [m["before"] for m in plan["mutations"]], now=created)
    if rebuilt != plan or plan["policy_sha256"] != POLICY_HASH:
        raise ValueError("archive plan lineage mismatch")


class ArchiveEngine:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def _path(self, plan):
        return self.state_dir / ("archive-" + plan["content_hash"] + ".json")

    def _persist(self, plan, receipt):
        _atomic_write_private_json(self._path(plan), receipt, prefix=".tmp-archive-")

    def apply(self, plan: dict, approval: dict, provider) -> dict:
        now = datetime.now(timezone.utc)
        validate_plan(plan, now)
        # There is intentionally no account auto-apply mode until reviewed
        # canary receipts and provider capability parity exist.
        selected = approval.get("selected_ids", [])
        if (approval.get("schema") != "uma.archive_canary_approval.v1"
                or approval.get("plan_sha256") != plan["content_hash"]
                or approval.get("policy_sha256") != POLICY_HASH
                or approval.get("authority") != "explicit_operator_selection"
                or not approval.get("selection_receipt")
                or not 1 <= len(selected) <= 3 or len(set(selected)) != len(selected)):
            raise ValueError("explicit 1–3-message canary selection required")
        by_id = {m["id"]: m for m in plan["mutations"]}
        if any(i not in by_id for i in selected):
            raise ValueError("selection does not belong to this plan")
        required = ("observe_archive", "archive_if_unchanged", "restore_archive_if_unchanged")
        if any(not callable(getattr(provider, name, None)) for name in required):
            return {"status": "blocked", "reason": "conditional_archive_adapter_unavailable", "writes_performed": 0}
        with AdvisoryFileLock(self.state_dir / "archive.lock"):
            if self._path(plan).exists():
                return {"status": "blocked", "reason": "replay_requires_reconciliation", "writes_performed": 0}
            receipt = {"schema": "uma.archive_receipt.v1", "plan_sha256": plan["content_hash"],
                       "approval_sha256": sha256_hex(approval), "writes_performed": 0,
                       "status": "prepared", "results": [{"id": i, "status": "unattempted"} for i in selected]}
            self._persist(plan, receipt)
            deadline = time.monotonic() + 600
            # All-or-zero preflight before the first intent reaches dispatch.
            for row in receipt["results"]:
                mutation = by_id[row["id"]]
                try:
                    current = provider.observe_archive(mutation)
                except Exception:
                    row["status"] = "failed"
                    receipt["status"] = "blocked"
                    self._persist(plan, receipt)
                    return receipt
                if current != mutation["before"]:
                    row["status"] = "conflicted"
                    receipt["status"] = "blocked"
                    self._persist(plan, receipt)
                    return receipt
            for row in receipt["results"]:
                mutation = by_id[row["id"]]
                if time.monotonic() >= deadline:
                    receipt["status"] = "run_ceiling"
                    break
                try:
                    if provider.observe_archive(mutation) != mutation["before"]:
                        row["status"] = "conflicted"
                        receipt["status"] = "conflicted"
                        break
                except Exception:
                    row["status"] = "failed"
                    receipt["status"] = "failed"
                    break
                row["status"] = "intent"
                self._persist(plan, receipt)  # A failure here prevents dispatch.
                row["status"] = "uncertain"
                receipt["writes_performed"] += 1  # Count possible dispatch even on timeout.
                try:
                    dispatched = provider.archive_if_unchanged(mutation)
                    if dispatched is False:
                        receipt["writes_performed"] -= 1
                        row["status"] = "conflicted"
                    elif dispatched is True:
                        for delay in (0, 2, 3):
                            if delay:
                                time.sleep(delay)
                            observed = provider.observe_archive(mutation)
                            status = verify_archive(provider=mutation["identity"]["provider"],
                                expected=MessageIdentity.model_validate(mutation["identity"]),
                                observed=observed, destination=mutation["destination"])
                            if status in ("verified", "conflicted"):
                                row["status"] = status
                                row["after"] = observed
                                break
                except Exception:
                    row["status"] = "uncertain"
                # Do not catch persistence failure and continue the batch.
                self._persist(plan, receipt)
                if row["status"] != "verified":
                    receipt["status"] = row["status"]
                    break
            else:
                receipt["status"] = "verified"
            self._persist(plan, receipt)
            return receipt

    def rollback(self, plan: dict, provider) -> dict:
        # Validate original lineage, allowing old plans for override-safe recovery.
        validate_plan(plan, datetime.fromisoformat(plan["created_at"]))
        if not callable(getattr(provider, "restore_archive_if_unchanged", None)):
            return {"status": "blocked", "reason": "conditional_archive_adapter_unavailable", "writes_performed": 0}
        with AdvisoryFileLock(self.state_dir / "archive.lock"):
            receipt = _json_object_from_path(self._path(plan), "archive receipt")
            if receipt.get("plan_sha256") != plan["content_hash"]:
                raise ValueError("rollback lineage mismatch")
            by_id = {m["id"]: m for m in plan["mutations"]}
            for row in receipt["results"]:
                if row["status"] != "verified":
                    continue
                mutation = by_id[row["id"]]
                current = provider.observe_archive(mutation)
                if current != row["after"]:
                    row["status"] = "rollback_conflicted"
                    self._persist(plan, receipt)
                    continue
                row["status"] = "rollback_intent"
                self._persist(plan, receipt)
                receipt["rollback_writes_performed"] = receipt.get("rollback_writes_performed", 0) + 1
                try:
                    dispatched = provider.restore_archive_if_unchanged(mutation, current)
                    row["status"] = "rollback_uncertain"
                    if dispatched is False:
                        receipt["rollback_writes_performed"] -= 1
                        row["status"] = "rollback_conflicted"
                    elif dispatched is True:
                        after = provider.observe_archive(mutation)
                        # Revision may advance; all original semantic fields must match.
                        comparable = {k: v for k, v in after.items() if k != "revision"}
                        before = {k: v for k, v in mutation["before"].items() if k != "revision"}
                        if comparable == before:
                            row["status"] = "rolled_back"
                except Exception:
                    row["status"] = "rollback_uncertain"
                self._persist(plan, receipt)
                if row["status"] == "rollback_uncertain":
                    break
            receipt["status"] = "rolled_back" if all(r["status"] == "rolled_back" for r in receipt["results"]) else "rollback_incomplete"
            self._persist(plan, receipt)
            return receipt

    def verify(self, plan: dict, provider) -> dict:
        validate_plan(plan, datetime.fromisoformat(plan["created_at"]))
        if not callable(getattr(provider, "observe_archive", None)):
            return {"status": "blocked", "reason": "server_archive_proof_unavailable", "writes_performed": 0}
        results = []
        for mutation in plan["mutations"]:
            try:
                observed = provider.observe_archive(mutation)
                status = verify_archive(provider=mutation["identity"]["provider"],
                    expected=MessageIdentity.model_validate(mutation["identity"]),
                    observed=observed, destination=mutation["destination"])
            except Exception:
                status = "uncertain"
            results.append({"id": mutation["id"], "status": status})
        return {"schema": "uma.archive_verification.v1", "plan_sha256": plan["content_hash"],
                "results": results, "writes_performed": 0, "replay_authorized": False}


def cmd_archive(args):
    import json
    from providers.gmail import GmailProvider
    from providers.imap import IMAPProvider
    try:
        source = _json_object_from_path(Path(args.input).expanduser(), "archive input")
        if args.operation == "plan":
            result = build_plan([Obligation.model_validate(o) for o in source["obligations"]],
                                source["archive_observations"], now=datetime.now(timezone.utc))
        else:
            engine = ArchiveEngine(Path(args.state_dir).expanduser())
            provider = GmailProvider() if args.provider == "gmail" else IMAPProvider()
            # Legacy providers fail the capability check before authentication.
            if args.operation == "apply":
                if not args.approval:
                    raise ValueError("--approval is required")
                approval = _json_object_from_path(Path(args.approval).expanduser(), "archive approval")
                result = engine.apply(source, approval, provider)
            elif args.operation == "rollback":
                result = engine.rollback(source, provider)
            else:
                result = engine.verify(source, provider)
        _atomic_write_private_json(Path(args.output).expanduser(), result, prefix=".tmp-archive-cli-")
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}))
        return 2
    print(json.dumps({"status": result.get("status", "written"), "output": args.output,
                      "reason": result.get("reason"), "writes_performed": result.get("writes_performed", 0)}))
    return 20 if result.get("status") == "blocked" else 0
