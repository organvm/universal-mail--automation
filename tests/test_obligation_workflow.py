from datetime import datetime, timedelta, timezone

import pytest

from core.obligation_workflow import (
    Evidence, MessageIdentity, Obligation, derive, import_legacy, reconcile, verify_archive,
)

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
MSG = MessageIdentity(provider="gmail", account="test@example.invalid", message_id="one",
                      evidence_digest="a" * 64)


def event(fact="request", **changes):
    return Evidence.model_validate(dict(id="e1", obligation_id="o1", message=MSG.model_dump(),
        occurred_at=NOW - timedelta(hours=1), observed_at=NOW, source="correspondence",
        fact=fact, next_actor="operator", next_action="Provide the requested detail",
        proof="exact-source-receipt", **changes))


def obligation(*events, **changes):
    return Obligation(id="o1", title="One task", thread_id="thread1", messages=(MSG,),
                      evidence=events, **changes)


def test_unknown_is_research_not_invented_owner():
    row = derive(obligation(), now=NOW)
    assert row["posture"] == "REVIEW"
    assert row["next_actor"] == "unknown"
    assert all(row["questions"][0][k] for k in ("question", "resolver", "checkpoint"))


def test_past_appointment_needs_chronology():
    row = derive(obligation(event("commitment", deadline=NOW-timedelta(days=1))), now=NOW)
    assert row["posture"] == "REVIEW"


def test_deadline_requires_consequence_and_owner():
    assert derive(obligation(event(deadline=NOW)), now=NOW)["posture"] == "ACTION"
    assert derive(obligation(event(deadline=NOW, consequential=True)), now=NOW)["posture"] == "NOW"


def test_waiting_requires_actual_correspondence():
    base = event("waiting").model_dump()
    base.update(next_actor="other", source="provider")
    assert derive(obligation(Evidence(**base)), now=NOW)["posture"] == "REVIEW"
    base["source"] = "correspondence"
    assert derive(obligation(Evidence(**base)), now=NOW)["posture"] == "WAITING"


def test_new_evidence_reopens_same_obligation():
    closed = event("completed")
    reopened = closed.model_copy(update={"id": "e2", "fact": "reopened", "occurred_at": NOW})
    assert derive(obligation(closed, reopened), now=NOW)["posture"] == "ACTION"


def test_distinct_verification_request_cannot_close_sibling():
    first = obligation(event("completed"))
    second = Obligation(id="o2", title="Different verification", thread_id="thread1", messages=(MSG,))
    result = reconcile([first, second], now=NOW)
    assert result["metrics"]["unresolved"] == 1
    assert result["obligations"][1]["posture"] == "REVIEW"
    with pytest.raises(ValueError, match="different obligation"):
        Obligation(id="o2", title="Different", thread_id="thread1", messages=(MSG,), evidence=first.evidence)


def test_active_sibling_and_protection_prevent_archive():
    non_action = obligation(event("non_action"))
    assert reconcile([non_action], now=NOW)["obligations"][0]["archive_eligible"]
    sibling = Obligation(id="o2", title="Still open", thread_id="thread1", messages=(MSG,))
    assert not reconcile([non_action, sibling], now=NOW)["obligations"][0]["archive_eligible"]
    for field in ("protected", "human_override"):
        assert not reconcile([non_action.model_copy(update={field: True})], now=NOW)["obligations"][0]["archive_eligible"]


def test_stale_completion_does_not_hide_work():
    old = event("completed").model_copy(update={"occurred_at": NOW-timedelta(days=3), "observed_at": NOW-timedelta(days=2)})
    assert derive(obligation(old), now=NOW)["posture"] == "REVIEW"


def test_legacy_adapter_preserves_unknown_aggregates():
    obs = import_legacy({"obligations": [{"title": "Old", "accounts": ["a", "b"], "is_starred": True}]}, now=NOW)
    assert len(obs) == 2
    assert {derive(o, now=NOW)["posture"] for o in obs} == {"REVIEW"}


def test_duplicate_identity_rejected():
    with pytest.raises(ValueError, match="duplicate message"):
        Obligation(id="o", title="x", thread_id="t", messages=(MSG, MSG))


def test_provider_specific_archive_proof():
    observed = {"identity": MSG.model_dump(), "server_confirmed": True, "label_ids": []}
    assert verify_archive(provider="gmail", expected=MSG, observed=observed) == "verified"
    cloud = MSG.model_copy(update={"provider": "icloud"})
    cloud_observed = {"identity": cloud.model_dump(), "server_confirmed": True}
    assert verify_archive(provider="icloud", expected=cloud, observed=cloud_observed, destination="Archive") == "uncertain"
    cloud_observed["mailboxes"] = ["Archive"]
    assert verify_archive(provider="icloud", expected=cloud, observed=cloud_observed, destination="Archive") == "verified"
    observed["server_confirmed"] = False
    assert verify_archive(provider="gmail", expected=MSG, observed=observed) == "uncertain"


def test_missing_labels_or_wrong_identity_are_not_archive_proof():
    assert verify_archive(provider="gmail", expected=MSG, observed={"identity": MSG.model_dump(), "server_confirmed": True}) == "uncertain"
    assert verify_archive(provider="gmail", expected=MSG, observed={}) == "conflicted"
