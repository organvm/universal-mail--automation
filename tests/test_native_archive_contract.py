from datetime import datetime, timezone

import pytest

from core.archive_transactions import ArchiveEngine, build_plan, verify_observation
from core.mail_inventory import seal
from core.flag_workflow import sha256_hex
from core.obligation_workflow import MessageIdentity, Obligation, POLICY_HASH
from tests.test_archive_transactions import setup, Provider


def v2_plan():
    plan, approval = setup(1)
    before = plan["mutations"][0]["before"]
    before.update(message_present=True, preserved_sha256="c" * 64)
    coverage = seal({"schema": "uma.archive_coverage.v2", "complete": True, "questions": [],
        "reviewed_obligations": {o["id"]: sha256_hex(o) for o in plan["obligations"]},
        "inventory_receipts": ["a" * 64], "research_receipts": ["b" * 64],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "message_keys": [MessageIdentity.model_validate(before["identity"]).key]})
    plan = build_plan([Obligation.model_validate(o) for o in plan["obligations"]], [before],
                      now=datetime.now(timezone.utc), coverage=coverage)
    approval.update(plan_sha256=plan["content_hash"], policy_sha256=POLICY_HASH,
                    selected_ids=[plan["mutations"][0]["id"]])
    return plan, approval


class Native(Provider):
    archive_contract = {"schema": "uma.archive_adapter.v2", "server_revision_precondition": "none"}

    def dispatch_archive(self, mutation):
        return {"status": "applied" if self.archive_if_unchanged(mutation) else "not_dispatched"}

    def restore_archive(self, mutation, expected):
        return {"status": "applied" if self.restore_archive_if_unchanged(mutation, expected) else "not_dispatched"}


def test_native_adapter_requires_coverage_plan(tmp_path):
    plan, approval = setup(1)
    with pytest.raises(ValueError, match="coverage"):
        ArchiveEngine(tmp_path).apply(plan, approval, Native(plan))


def test_typed_dispatch_and_settled_preservation(tmp_path, monkeypatch):
    monkeypatch.setattr("core.archive_transactions.time.sleep", lambda _: None)
    plan, approval = v2_plan()
    result = ArchiveEngine(tmp_path).apply(plan, approval, Native(plan))
    assert result["status"] == "verified"
    assert result["adapter_contract"]["server_revision_precondition"] == "none"
    assert result["results"][0]["dispatch"]["status"] == "applied"


def test_ambiguous_dispatch_freezes_without_retry(tmp_path):
    plan, approval = v2_plan()
    provider = Native(plan)
    provider.dispatch_archive = lambda mutation: {"status": "ambiguous"}
    engine = ArchiveEngine(tmp_path)
    result = engine.apply(plan, approval, provider)
    assert result["status"] == "uncertain"
    assert result["writes_performed"] == 1
    assert engine.apply(plan, approval, provider)["reason"] == "replay_requires_reconciliation"


def test_unrelated_metadata_change_conflicts_even_when_archive_present():
    plan, _ = v2_plan()
    mutation = plan["mutations"][0]
    observed = {**mutation["before"], "in_inbox": False, "label_ids": [], "preserved_sha256": "d" * 64}
    assert verify_observation(mutation, observed, native_v2=True) == "conflicted"
