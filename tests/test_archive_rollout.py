from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from core.archive_transactions import ArchiveEngine, build_plan
from core.flag_workflow import sha256_hex
from core.mail_inventory import seal
from core.obligation_workflow import Evidence, MessageIdentity, Obligation, POLICY_HASH
from tests.test_archive_transactions import setup
from tests.test_native_archive_contract import Native


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("core.archive_transactions.time.sleep", lambda _: None)


def native_plan(count=1, offset=0):
    now = datetime.now(timezone.utc)
    obligations, observations = [], []
    for index in range(offset, offset + count):
        identity = MessageIdentity(provider="gmail", account="a@example.invalid", message_id=str(index), evidence_digest="a" * 64)
        evidence = Evidence(id=f"e{index}", obligation_id=str(index), message=identity,
                            occurred_at=now, observed_at=now, source="correspondence",
                            fact="non_action", proof="reviewed-receipt")
        obligations.append(Obligation(id=str(index), title="Non-action receipt", thread_id=str(index),
                                      messages=(identity,), evidence=(evidence,)))
        observations.append({"identity": identity.model_dump(), "server_confirmed": True, "in_inbox": True,
                             "label_ids": ["INBOX"], "revision": "1", "protected": False,
                             "human_override": False, "message_present": True,
                             "preserved_sha256": sha256_hex(identity.model_dump())})
    coverage = seal({"schema": "uma.archive_coverage.v2", "complete": True, "questions": [],
        "reviewed_obligations": {o.id: sha256_hex(o.model_dump(mode="json")) for o in obligations},
        "inventory_receipts": ["a" * 64], "research_receipts": ["b" * 64], "observed_at": now.isoformat(),
        "message_keys": [MessageIdentity.model_validate(b["identity"]).key for b in observations]})
    return build_plan(obligations, observations, now=now, coverage=coverage)


def rollout(tmp_path, plans):
    canary = native_plan()
    approval = {"schema": "uma.archive_canary_approval.v1", "plan_sha256": canary["content_hash"],
        "policy_sha256": POLICY_HASH, "authority": "explicit_operator_selection",
        "selection_receipt": "fixture-exact-canary-selection", "selected_ids": [canary["mutations"][0]["id"]]}
    engine = ArchiveEngine(tmp_path)
    receipt = engine.apply(canary, approval, Native(canary))
    now = datetime.now(timezone.utc)
    scope = seal({"schema": "uma.archive_rollout_approval.v1", "authority": "explicit_operator_rollout",
        "policy_sha256": POLICY_HASH, "account": "a@example.invalid", "provider": "gmail",
        "created_at": now.isoformat(), "expires_at": (now + timedelta(hours=1)).isoformat(),
        "selection_receipt": "fixture-explicit-reviewed-rollout", "canary_plan": canary,
        "canary_receipt_sha256": sha256_hex(receipt),
        "plans": {p["content_hash"]: [m["id"] for m in p["mutations"]] for p in plans}})
    return engine, scope


def test_verified_canary_authorizes_exact_25_message_batch(tmp_path):
    plan = native_plan(25, offset=1)
    engine, approval = rollout(tmp_path, [plan])
    provider = Native(plan)
    receipt = engine.apply(plan, approval, provider)
    assert receipt["status"] == "verified" and receipt["writes_performed"] == provider.writes == 25
    assert receipt["authorization_kind"] == "uma.archive_rollout_approval.v1"
    assert engine.apply(plan, approval, provider)["reason"] == "replay_requires_reconciliation"


@pytest.mark.parametrize("change", ["account", "expired", "receipt", "scope", "too_many", "authority"])
def test_invalid_rollout_authority_never_writes(tmp_path, change):
    plan = native_plan(4, offset=1)
    engine, approval = rollout(tmp_path, [plan])
    if change == "account":
        approval["account"] = "another@example.invalid"
    elif change == "expired":
        approval["expires_at"] = approval["created_at"]
    elif change == "receipt":
        approval["canary_receipt_sha256"] = "0" * 64
    elif change == "scope":
        approval["plans"] = {"0" * 64: ["unrelated"]}
    elif change == "too_many":
        approval["plans"][plan["content_hash"]] = [str(i) for i in range(26)]
    else:
        approval["authority"] = "recorded_task"
    seal(approval)
    provider = Native(plan)
    with pytest.raises(ValueError):
        engine.apply(plan, approval, provider)
    assert provider.writes == 0


def test_changed_plan_cannot_replay_same_identity_under_one_scope(tmp_path):
    plan = native_plan(1, offset=1)
    other = build_plan([Obligation.model_validate(o) for o in plan["obligations"]],
        [m["before"] for m in plan["mutations"]], now=datetime.now(timezone.utc), coverage=deepcopy(plan["coverage"]))
    engine, approval = rollout(tmp_path, [plan, other])
    assert engine.apply(plan, approval, Native(plan))["status"] == "verified"
    provider = Native(other)
    assert engine.apply(other, approval, provider)["reason"] == "scope_identity_requires_reconciliation"
    assert provider.writes == 0


def test_ambiguous_rollout_stops_and_freezes_batch(tmp_path):
    plan = native_plan(4, offset=1)
    engine, approval = rollout(tmp_path, [plan])
    provider = Native(plan)
    provider.fail_at = 2
    receipt = engine.apply(plan, approval, provider)
    assert [r["status"] for r in receipt["results"]] == ["verified", "uncertain", "unattempted", "unattempted"]
    assert provider.writes == 2
    assert engine.apply(plan, approval, provider)["reason"] == "replay_requires_reconciliation"


def test_rolled_back_canary_cannot_authorize_rollout(tmp_path):
    plan = native_plan(4, offset=1)
    engine, approval = rollout(tmp_path, [plan])
    import json
    path = engine._path(approval["canary_plan"])
    receipt = json.loads(path.read_text())
    receipt["status"] = "rolled_back"
    path.write_text(json.dumps(receipt))
    approval["canary_receipt_sha256"] = sha256_hex(receipt)
    seal(approval)
    provider = Native(plan)
    with pytest.raises(ValueError, match="canary"):
        engine.apply(plan, approval, provider)
    assert provider.writes == 0
