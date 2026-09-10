from types import SimpleNamespace

import pytest

from core.flag_verification import verify_transactions
from core.obligation_workflow import Evidence, MessageIdentity, Obligation, flag_candidates, reconcile
from datetime import datetime, timezone
from tests.test_commit6_transactions import Harness


def test_read_only_verification_preserves_ledger_and_does_not_authorize_replay(tmp_path):
    h = Harness(tmp_path)
    assert h.apply().status == "applied"
    before = h.ledger.path.read_bytes()
    h.provider.get_message_details_ref = lambda ref: SimpleNamespace(is_starred=True)
    h.provider.calls.clear()
    receipt = verify_transactions(h.plan, h.ledger, h.provider)
    assert all(r["status"] == "verified" for r in receipt["results"])
    assert receipt["writes_performed"] == 0
    assert receipt["replay_authorized"] is False
    assert before == h.ledger.path.read_bytes()
    assert all(c[0] == "resolve_scoped" for c in h.provider.calls)


def test_color_without_flagged_status_never_verifies(tmp_path):
    h = Harness(tmp_path)
    h.apply()
    h.provider.get_message_details_ref = lambda ref: SimpleNamespace(is_starred=False)
    receipt = verify_transactions(h.plan, h.ledger, h.provider)
    assert all(r["status"] == "conflicted" for r in receipt["results"])


def test_absent_transactions_are_not_success(tmp_path):
    h = Harness(tmp_path)
    with pytest.raises(ValueError, match="no recorded"):
        verify_transactions(h.plan, h.ledger, h.provider)


def test_obligation_bridge_preserves_plan_and_requires_selection(tmp_path):
    h = Harness(tmp_path)
    mutation = h.mutations[0]
    now = datetime.now(timezone.utc)
    msg = MessageIdentity(provider=mutation.provider, account=mutation.account,
                          message_id=mutation.provider_id, evidence_digest=mutation.evidence_digest)
    evidence = Evidence(id="e", obligation_id="o", message=msg, occurred_at=now, observed_at=now,
                        source="correspondence", fact="request", next_actor="operator",
                        next_action="Provide details", proof="reviewed-source")
    ob = Obligation(id="o", title="Independent task", thread_id="t", messages=(msg,), evidence=(evidence,))
    view = reconcile([ob], now=now)
    original_hash = h.plan["plan_hash"]
    candidate = flag_candidates(view, h.plan)["candidates"][0]
    assert candidate["mutation_id"] == mutation.mutation_id
    assert candidate["target_flag"] == "orange"
    assert candidate["selection_status"] == "requires_explicit_canary_selection"
    assert h.plan["plan_hash"] == original_hash
    sibling = Obligation(id="sibling", title="Unknown sibling", thread_id="t", messages=(msg,))
    candidate = flag_candidates(reconcile([ob, sibling], now=now), h.plan)["candidates"][0]
    assert candidate["target_flag"] == "purple"
    assert candidate["selection_status"] == "blocked"
