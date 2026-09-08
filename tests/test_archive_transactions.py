from copy import deepcopy
from datetime import datetime, timezone

import pytest

from core.archive_transactions import ArchiveEngine, build_plan
from core.obligation_workflow import Evidence, MessageIdentity, Obligation, POLICY_HASH


@pytest.fixture(autouse=True)
def no_verification_sleep(monkeypatch):
    monkeypatch.setattr("core.archive_transactions.time.sleep", lambda _: None)


def setup(count=2):
    now = datetime.now(timezone.utc)
    obs, obligations = [], []
    for i in range(count):
        msg = MessageIdentity(provider="gmail", account="a@example.invalid", message_id=str(i), evidence_digest="a"*64)
        evidence = Evidence(id=f"e{i}", obligation_id=str(i), message=msg, occurred_at=now, observed_at=now,
                            source="correspondence", fact="non_action", proof="reviewed-receipt")
        obligations.append(Obligation(id=str(i), title="Non-action receipt", thread_id=str(i), messages=(msg,), evidence=(evidence,)))
        obs.append({"identity": msg.model_dump(), "server_confirmed": True, "in_inbox": True,
                    "label_ids": ["INBOX"], "revision": "1", "protected": False, "human_override": False})
    plan = build_plan(obligations, obs, now=now)
    approval = {"schema": "uma.archive_canary_approval.v1", "plan_sha256": plan["content_hash"],
                "policy_sha256": POLICY_HASH, "authority": "explicit_operator_selection",
                "selection_receipt": "operator-test-selection", "selected_ids": [m["id"] for m in plan["mutations"]]}
    return plan, approval


class Provider:
    def __init__(self, plan):
        self.states = {m["id"]: deepcopy(m["before"]) for m in plan["mutations"]}
        self.writes = 0
        self.fail_at = None

    def observe_archive(self, mutation):
        return deepcopy(self.states[mutation["id"]])

    def archive_if_unchanged(self, mutation):
        self.writes += 1
        if self.writes == self.fail_at:
            raise RuntimeError("dispatch timeout")
        if self.states[mutation["id"]] != mutation["before"]:
            return False
        self.states[mutation["id"]].update(in_inbox=False, label_ids=[], revision="2")
        return True

    def restore_archive_if_unchanged(self, mutation, expected):
        if self.states[mutation["id"]] != expected:
            return False
        self.writes += 1
        self.states[mutation["id"]] = {**mutation["before"], "revision": "3"}
        return True


def test_archive_proof_replay_and_override_safe_rollback(tmp_path):
    plan, approval = setup()
    provider = Provider(plan)
    engine = ArchiveEngine(tmp_path / "state")
    result = engine.apply(plan, approval, provider)
    assert result["status"] == "verified"
    assert result["writes_performed"] == 2
    assert engine.apply(plan, approval, provider)["reason"] == "replay_requires_reconciliation"
    provider.states[plan["mutations"][0]["id"]]["revision"] = "human-edit"
    result = engine.rollback(plan, provider)
    assert [r["status"] for r in result["results"]] == ["rollback_conflicted", "rolled_back"]
    assert provider.writes == 3


def test_complete_batch_preflight_prevents_any_write(tmp_path):
    plan, approval = setup()
    provider = Provider(plan)
    provider.states[plan["mutations"][1]["id"]]["revision"] = "human-edit"
    result = ArchiveEngine(tmp_path / "state").apply(plan, approval, provider)
    assert result["writes_performed"] == provider.writes == 0
    assert [r["status"] for r in result["results"]] == ["unattempted", "conflicted"]


def test_partial_failure_stops_remaining_writes(tmp_path):
    plan, approval = setup(3)
    provider = Provider(plan)
    provider.fail_at = 2
    result = ArchiveEngine(tmp_path / "state").apply(plan, approval, provider)
    assert [r["status"] for r in result["results"]] == ["verified", "uncertain", "unattempted"]
    assert result["writes_performed"] == 2


@pytest.mark.parametrize("when,expected_writes", [("intent", 0), ("verified", 1)])
def test_receipt_failure_halts_writes(tmp_path, monkeypatch, when, expected_writes):
    plan, approval = setup()
    provider = Provider(plan)
    engine = ArchiveEngine(tmp_path / "state")
    persist = engine._persist
    def fail(plan, receipt):
        if any(r["status"] == when for r in receipt["results"]):
            raise OSError("disk full")
        persist(plan, receipt)
    monkeypatch.setattr(engine, "_persist", fail)
    with pytest.raises(OSError):
        engine.apply(plan, approval, provider)
    assert provider.writes == expected_writes


def test_legacy_boolean_provider_cannot_bypass_capability_gate(tmp_path):
    plan, approval = setup()
    result = ArchiveEngine(tmp_path / "state").apply(plan, approval, object())
    assert result["reason"] == "conditional_archive_adapter_unavailable"
    assert result["writes_performed"] == 0


def test_wrong_obligation_identity_does_not_authorize_archive():
    plan, _ = setup()
    observations = [m["before"] for m in plan["mutations"]]
    observations[0]["identity"]["evidence_digest"] = "b"*64
    with pytest.raises(ValueError, match="evidence-backed"):
        build_plan([Obligation.model_validate(o) for o in plan["obligations"]], observations,
                   now=datetime.now(timezone.utc))


def test_archive_late_inbox_reversion_is_not_verified(tmp_path, monkeypatch):
    plan, approval = setup(1)
    provider = Provider(plan)
    calls = []
    def sleep(delay):
        calls.append(delay)
        if delay == 3:
            provider.states[plan["mutations"][0]["id"]].update(in_inbox=True, label_ids=["INBOX"])
    monkeypatch.setattr("core.archive_transactions.time.sleep", sleep)
    result = ArchiveEngine(tmp_path / "state").apply(plan, approval, provider)
    assert result["status"] == "unchanged"
    assert calls == [2, 3]
    assert provider.writes == 1


def test_reconciliation_is_persisted_without_dispatch(tmp_path):
    import json
    plan, _ = setup(1)
    provider = Provider(plan)
    engine = ArchiveEngine(tmp_path / "state")
    result = engine.verify(plan, provider)
    stored = list((tmp_path / "state").glob("verification-*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text()) == result
    assert provider.writes == 0
    assert result["replay_authorized"] is False
