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


def verify_observation(mutation, observed, *, native_v2=False):
    status = verify_archive(provider=mutation["identity"]["provider"],
        expected=MessageIdentity.model_validate(mutation["identity"]),
        observed=observed, destination=mutation["destination"])
    if native_v2 and (observed.get("message_present") is not True or
            not observed.get("preserved_sha256") or observed["preserved_sha256"] != mutation["before"].get("preserved_sha256")
            or observed.get("human_override") is not False or observed.get("protected") is not False):
        return "conflicted"
    return status


def build_plan(obligations: list[Obligation], observations: list[dict], *, now: datetime,
               coverage: dict | None = None) -> dict:
    rows = reconcile(obligations, now=now)["obligations"]
    eligible = {sha256_hex(m)
                for r in rows if r["archive_eligible"] for m in r["messages"]}
    mutations = []
    for observed in observations:
        identity = MessageIdentity.model_validate(observed["identity"])
        if identity.provider not in ("gmail", "icloud", "outlook"):
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
        if identity.provider in ("icloud", "outlook") and not mutation["destination"]:
            raise ValueError("folder provider archive destination required")
        mutation["id"] = "archive-" + sha256_hex(mutation)
        mutations.append(mutation)
    if not 1 <= len(mutations) <= 25 or len({m["identity"]["account"] for m in mutations}) != 1:
        raise ValueError("archive plan must contain 1–25 messages in one account")
    if len({sha256_hex(m["identity"]) for m in mutations}) != len(mutations):
        raise ValueError("duplicate archive identity")
    plan = {"schema": "uma.archive_plan.v1", "policy_sha256": POLICY_HASH,
            "created_at": now.isoformat(), "mutations": mutations,
            "obligations": [o.model_dump(mode="json") for o in obligations]}
    if coverage is not None:
        from core.mail_inventory import validate
        validate(coverage)
        if (coverage.get("schema") not in ("uma.archive_coverage.v1", "uma.archive_coverage.v2") or coverage.get("complete") is not True
                or coverage.get("questions") or not coverage.get("inventory_receipts")
                or not coverage.get("research_receipts")):
            raise ValueError("complete inventory and research coverage evidence required")
        checked = datetime.fromisoformat(coverage["observed_at"])
        if checked.tzinfo is None or not timedelta(0) <= now - checked <= timedelta(hours=24):
            raise ValueError("coverage evidence is stale")
        covered = set(coverage["message_keys"])
        from core.flag_workflow import require_hex256
        for field in ("inventory_receipts", "research_receipts", "message_keys"):
            if not isinstance(coverage[field], list) or not coverage[field]:
                raise ValueError("coverage receipt lists cannot be empty")
            for digest in coverage[field]:
                require_hex256(digest, "coverage " + field)
        if any(MessageIdentity.model_validate(m["identity"]).key not in covered for m in mutations):
            raise ValueError("archive identity not covered by research")
        if coverage["schema"] == "uma.archive_coverage.v2":
            reviewed = {o.id: sha256_hex(o.model_dump(mode="json")) for o in obligations}
            if reviewed != coverage["reviewed_obligations"]:
                raise ValueError("archive plan must retain the complete reviewed obligation set")
        plan.update(schema="uma.archive_plan.v2", coverage=coverage)
    plan["content_hash"] = sha256_hex(plan)
    return plan


def validate_plan(plan: dict, now: datetime) -> None:
    created = datetime.fromisoformat(plan["created_at"])
    if created.tzinfo is None or not timedelta(0) <= now - created <= timedelta(hours=24):
        raise ValueError("archive plan is stale or future-dated")
    rebuilt = build_plan([Obligation.model_validate(o) for o in plan["obligations"]],
                         [m["before"] for m in plan["mutations"]], now=created, coverage=plan.get("coverage"))
    if rebuilt != plan or plan["policy_sha256"] != POLICY_HASH:
        raise ValueError("archive plan lineage mismatch")


class ArchiveEngine:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir

    def _path(self, plan):
        return self.state_dir / ("archive-" + plan["content_hash"] + ".json")

    def _persist(self, plan, receipt):
        receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_private_json(self._path(plan), receipt, prefix=".tmp-archive-")

    def _approved_selection(self, plan, approval, provider, now):
        """Validate concrete canary or same-account, canary-backed rollout scope."""
        if approval.get("schema") == "uma.archive_canary_approval.v1":
            selected = approval.get("selected_ids", [])
            if (approval.get("plan_sha256") != plan["content_hash"]
                    or approval.get("policy_sha256") != POLICY_HASH
                    or approval.get("authority") != "explicit_operator_selection"
                    or not approval.get("selection_receipt")
                    or not isinstance(selected, list) or not 1 <= len(selected) <= 3
                    or any(not isinstance(i, str) for i in selected) or len(set(selected)) != len(selected)):
                raise ValueError("explicit 1–3-message canary selection required")
            return selected
        required = {"schema", "authority", "policy_sha256", "account", "provider", "created_at", "expires_at",
                    "selection_receipt", "plans", "canary_plan", "canary_receipt_sha256", "content_hash"}
        if (set(approval) != required or approval.get("schema") != "uma.archive_rollout_approval.v1"
                or approval.get("authority") != "explicit_operator_rollout"
                or approval.get("policy_sha256") != POLICY_HASH or not approval.get("selection_receipt")
                or approval.get("content_hash") != sha256_hex({k: v for k, v in approval.items() if k != "content_hash"})):
            raise ValueError("explicit scoped rollout authorization required")
        created, expires = (datetime.fromisoformat(approval[k]) for k in ("created_at", "expires_at"))
        if (created.tzinfo is None or expires.tzinfo is None or not created <= now < expires
                or not timedelta(0) < expires - created <= timedelta(hours=24)):
            raise ValueError("rollout authorization is expired or future-dated")
        scope = {(m["identity"]["provider"], m["identity"]["account"]) for m in plan["mutations"]}
        if scope != {(approval["provider"], approval["account"])}:
            raise ValueError("rollout account scope mismatch")
        contract = getattr(provider, "archive_contract", {})
        if not isinstance(contract, dict) or contract.get("schema") != "uma.archive_adapter.v2":
            raise ValueError("rollout requires a native archive adapter")
        from core.flag_workflow import require_hex256
        plans = approval["plans"]
        if not isinstance(plans, dict) or not plans:
            raise ValueError("rollout must name exact previewed plans")
        for digest, selected in plans.items():
            require_hex256(digest, "rollout plan hash")
            if (not isinstance(selected, list) or not 1 <= len(selected) <= 25
                    or any(not isinstance(i, str) for i in selected) or len(set(selected)) != len(selected)):
                raise ValueError("rollout batches require 1–25 exact unique mutation IDs")
        selected = plans.get(plan["content_hash"])
        if not selected:
            raise ValueError("plan is outside the approved rollout scope")
        canary = approval["canary_plan"]
        validate_plan(canary, datetime.fromisoformat(canary["created_at"]))
        if {(m["identity"]["provider"], m["identity"]["account"]) for m in canary["mutations"]} != scope:
            raise ValueError("verified canary must belong to the rollout account")
        receipt = _json_object_from_path(self._path(canary), "verified archive canary receipt")
        if (sha256_hex(receipt) != approval["canary_receipt_sha256"]
                or receipt.get("plan_sha256") != canary["content_hash"] or receipt.get("status") != "verified"
                or receipt.get("authorization_kind") != "uma.archive_canary_approval.v1"
                or receipt.get("adapter_contract") != contract
                or not 1 <= len(receipt.get("results", [])) <= 3
                or any(r.get("status") != "verified" for r in receipt["results"])):
            raise ValueError("verified native canary receipt does not match approval")
        checked = datetime.fromisoformat(receipt["verified_at"])
        if checked.tzinfo is None or not timedelta(0) <= now - checked <= timedelta(hours=24):
            raise ValueError("verified canary receipt is stale")
        canary_by_id = {m["id"]: m for m in canary["mutations"]}
        if any(r["id"] not in canary_by_id or verify_observation(canary_by_id[r["id"]], r["after"], native_v2=True) != "verified"
               for r in receipt["results"]):
            raise ValueError("canary receipt lacks exact preserved-message proof")
        used = {MessageIdentity.model_validate(canary_by_id[r["id"]]["identity"]).key for r in receipt["results"]}
        if any(MessageIdentity.model_validate(m["identity"]).key in used for m in plan["mutations"] if m["id"] in selected):
            raise ValueError("rollout cannot replay its canary identities")
        return selected

    def apply(self, plan: dict, approval: dict, provider) -> dict:
        now = datetime.now(timezone.utc)
        validate_plan(plan, now)
        selected = self._approved_selection(plan, approval, provider, now)
        by_id = {m["id"]: m for m in plan["mutations"]}
        if any(i not in by_id for i in selected):
            raise ValueError("selection does not belong to this plan")
        contract = getattr(provider, "archive_contract", None)
        native_v2 = isinstance(contract, dict) and contract.get("schema") == "uma.archive_adapter.v2"
        if native_v2 and (plan["schema"] != "uma.archive_plan.v2" or plan["coverage"]["schema"] != "uma.archive_coverage.v2"):
            raise ValueError("production archive requires a coverage-bound v2 plan")
        required = (("observe_archive", "dispatch_archive", "restore_archive") if native_v2 else
                    ("observe_archive", "archive_if_unchanged", "restore_archive_if_unchanged"))
        if any(not callable(getattr(provider, name, None)) for name in required):
            return {"status": "blocked", "reason": "conditional_archive_adapter_unavailable", "writes_performed": 0}
        with AdvisoryFileLock(self.state_dir / "archive.lock"):
            # Recheck canary lineage under the same lock used by rollback.
            self._approved_selection(plan, approval, provider, datetime.now(timezone.utc))
            if self._path(plan).exists():
                return {"status": "blocked", "reason": "replay_requires_reconciliation", "writes_performed": 0}
            identities = {i: MessageIdentity.model_validate(by_id[i]["identity"]).key for i in selected}
            if approval["schema"] == "uma.archive_rollout_approval.v1":
                for path in self.state_dir.glob("archive-*.json"):
                    previous = _json_object_from_path(path, "archive scope receipt")
                    if (previous.get("approval_sha256") == sha256_hex(approval)
                            and set(previous.get("identities", {}).values()) & set(identities.values())):
                        return {"status": "blocked", "reason": "scope_identity_requires_reconciliation", "writes_performed": 0}
            receipt = {"schema": "uma.archive_receipt.v1", "plan_sha256": plan["content_hash"],
                       "approval_sha256": sha256_hex(approval), "writes_performed": 0,
                       "authorization_kind": approval["schema"], "created_at": now.isoformat(), "identities": identities,
                       "status": "prepared", "results": [{"id": i, "status": "unattempted"} for i in selected]}
            if native_v2:
                receipt["adapter_contract"] = contract
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
                    dispatched = provider.dispatch_archive(mutation) if native_v2 else provider.archive_if_unchanged(mutation)
                    if native_v2:
                        row["dispatch"] = dispatched
                        dispatched = {"applied": True, "not_dispatched": False}.get(dispatched.get("status"))
                    if dispatched is False:
                        receipt["writes_performed"] -= 1
                        row["status"] = "conflicted"
                    elif dispatched is True:
                        for delay in (0, 2, 3):
                            if delay:
                                time.sleep(delay)
                            observed = provider.observe_archive(mutation)
                            status = verify_observation(mutation, observed, native_v2=native_v2)
                            row["status"] = status
                            row["after"] = observed
                            if status == "conflicted":
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
                receipt["verified_at"] = datetime.now(timezone.utc).isoformat()
            self._persist(plan, receipt)
            return receipt

    def rollback(self, plan: dict, provider) -> dict:
        # Validate original lineage, allowing old plans for override-safe recovery.
        validate_plan(plan, datetime.fromisoformat(plan["created_at"]))
        native_v2 = getattr(provider, "archive_contract", {}).get("schema") == "uma.archive_adapter.v2"
        restore = getattr(provider, "restore_archive" if native_v2 else "restore_archive_if_unchanged", None)
        if not callable(restore):
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
                    dispatched = restore(mutation, current)
                    if native_v2:
                        row["rollback_dispatch"] = dispatched
                        dispatched = {"applied": True, "not_dispatched": False}.get(dispatched.get("status"))
                    row["status"] = "rollback_uncertain"
                    if dispatched is False:
                        receipt["rollback_writes_performed"] -= 1
                        row["status"] = "rollback_conflicted"
                    elif dispatched is True:
                        after = provider.observe_archive(mutation)
                        # Revision may advance; all original semantic fields must match.
                        comparable = {k: v for k, v in after.items() if k != "revision"}
                        before = {k: v for k, v in mutation["before"].items() if k != "revision"}
                        if native_v2:
                            fields = ("identity", "server_confirmed", "message_present", "in_inbox", "mailboxes",
                                      "label_ids", "preserved_sha256", "protected", "human_override")
                            comparable = {k: after.get(k) for k in fields}
                            before = {k: mutation["before"].get(k) for k in fields}
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
        native_v2 = getattr(provider, "archive_contract", {}).get("schema") == "uma.archive_adapter.v2"
        for mutation in plan["mutations"]:
            try:
                observed = provider.observe_archive(mutation)
                status = verify_observation(mutation, observed, native_v2=native_v2)
            except Exception:
                status = "uncertain"
            results.append({"id": mutation["id"], "status": status})
        verification = {"schema": "uma.archive_verification.v1", "plan_sha256": plan["content_hash"],
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "results": results, "writes_performed": 0, "replay_authorized": False}
        verification["content_hash"] = sha256_hex(verification)
        with AdvisoryFileLock(self.state_dir / "archive.lock"):
            _atomic_write_private_json(
                self.state_dir / ("verification-" + verification["content_hash"] + ".json"),
                verification, prefix=".tmp-archive-verification-")
        return verification


def cmd_archive(args):
    import json
    from providers.archive_factory import archive_provider
    from core.archive_guard import ArchiveGuard
    from core.flag_activation import default_flags_state_dir
    dispatch_may_have_started = False
    try:
        source = _json_object_from_path(Path(args.input).expanduser(), "archive input")
        if args.operation == "plan":
            result = build_plan([Obligation.model_validate(o) for o in source["obligations"]],
                                source["archive_observations"], now=datetime.now(timezone.utc), coverage=source.get("coverage"))
        else:
            engine = ArchiveEngine(Path(args.state_dir).expanduser())
            if not args.guard_evidence:
                raise ValueError("--guard-evidence with exact override bindings is required")
            targets = source.get("mutations", [])
            if not targets or any(m["identity"]["provider"] != args.provider or
                                  m["identity"]["account"] != args.account for m in targets):
                raise ValueError("explicit provider/account does not match every archive target")
            guard = ArchiveGuard(Path(args.guard_evidence).expanduser(), default_flags_state_dir() / "overrides.json")
            # Validate all guard bindings before opening a provider connection.
            for mutation in targets:
                guard(mutation["identity"])
            with archive_provider(args, guard) as provider:
                if args.operation == "observe":
                    result = {"schema": "uma.archive_observations.v1",
                              "archive_observations": [provider.observe_archive(m) for m in targets],
                              "writes_performed": 0}
                elif args.operation == "apply":
                    if not args.approval:
                        raise ValueError("--approval is required")
                    approval = _json_object_from_path(Path(args.approval).expanduser(), "archive approval")
                    dispatch_may_have_started = True
                    result = engine.apply(source, approval, provider)
                elif args.operation == "rollback":
                    dispatch_may_have_started = True
                    result = engine.rollback(source, provider)
                else:
                    result = engine.verify(source, provider)
        _atomic_write_private_json(Path(args.output).expanduser(), result, prefix=".tmp-archive-cli-")
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "uncertain" if dispatch_may_have_started else "blocked",
                          "error_type": type(exc).__name__, "writes_performed": None if dispatch_may_have_started else 0,
                          "next_step": "inspect_persisted_receipts" if dispatch_may_have_started else "resolve_preflight"}))
        return 2
    print(json.dumps({"status": result.get("status", "written"), "output": args.output,
                      "reason": result.get("reason"), "writes_performed": result.get("writes_performed", 0)}))
    if args.operation == "apply":
        return 0 if result.get("status") == "verified" else 20
    if args.operation == "rollback":
        return 0 if result.get("status") == "rolled_back" else 20
    if args.operation in ("verify", "reconcile"):
        return 0 if result.get("results") and all(r["status"] == "verified" for r in result["results"]) else 20
    return 20 if result.get("status") == "blocked" else 0
