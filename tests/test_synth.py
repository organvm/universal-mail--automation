"""
Tests for the Modular Synth ecosystem extensions:
Identity resolution, channel-aware SLA, PatchBay routing, and Inbound/Dispatch endpoints.
"""

from datetime import datetime, timezone
from fastapi.testclient import TestClient

from api.app import app
from core.identity import Entity, EntityDirectory, DEFAULT_DIRECTORY
from core.models import CommAction, CommMessage
from core.patchbay import PatchBay, PatchCable, SinkType
from core.sla import compute_sla

client = TestClient(app)


def test_entity_directory_multi_channel():
    directory = EntityDirectory()
    directory.register(Entity(
        name="VIP Partner",
        emails={"partner@firm.com"},
        phones={"+1 415 555 9999"},
        handles={"@partner_slack"},
        is_protected=True,
        is_vip=True,
    ))

    # Resolves email
    assert directory.is_protected("partner@firm.com") is True
    # Resolves normalized phone
    assert directory.is_protected("+14155559999") is True
    assert directory.is_protected("(415) 555-9999") is True
    # Resolves chat handle
    assert directory.is_vip("@partner_slack") is True
    # Unknown sender
    assert directory.is_protected("unknown@nowhere.com") is False


def test_sla_channel_boost():
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    
    # Twilio SMS boosts priority tier by 1 and sets short deadline (15 mins)
    tier, deadline = compute_sla("twilio", base_tier=2, received_at=now)
    assert tier == 1
    assert (deadline - now).total_seconds() == 15 * 60

    # Email preserves base tier and sets 24-hour deadline
    tier_email, deadline_email = compute_sla("email", base_tier=2, received_at=now)
    assert tier_email == 2
    assert (deadline_email - now).total_seconds() == 24 * 3600


def test_patchbay_routing():
    patchbay = PatchBay()
    received = []

    cable = PatchCable(
        name="critical-alert-cable",
        sink_type=SinkType.CALLBACK,
        destination="alert-bus",
        min_tier=1,
        callback=lambda act, msg: received.append(act.message_id),
    )
    patchbay.connect(cable)

    # Action with Tier 1 triggers the cable
    action_crit = CommAction(message_id="msg-1", channel_id="slack", sender="alice")
    action_crit.priority_tier = 1
    results = patchbay.transmit(action_crit)
    assert len(results) == 1
    assert results[0]["status"] == "delivered"
    assert "msg-1" in received

    # Action with Tier 4 does NOT trigger
    action_low = CommAction(message_id="msg-2", channel_id="slack", sender="bob")
    action_low.priority_tier = 4
    results_low = patchbay.transmit(action_low)
    assert len(results_low) == 0


def test_inbound_twilio_endpoint():
    r = client.post("/v1/inbound/twilio", json={
        "From": "+18005551234",
        "Body": "Urgent alert from Chase bank",
        "MessageSid": "SM123456",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["channel_id"] == "twilio"
    assert data["is_protected"] is True  # In default identity directory
    assert data["archive"] is False
    assert data["star"] is True
    assert "sla_deadline" in data


def test_inbound_generic_endpoint():
    r = client.post("/v1/inbound/generic", json={
        "source": "slack",
        "sender": "@user123",
        "content": "Quarterly invoice attached for billing review",
        "event_id": "evt-789",
    })
    assert r.status_code == 200
    data = r.json()
    assert data["channel_id"] == "slack"
    assert "Finance" in data["add_labels"][0]


def test_dispatch_endpoint():
    from core.patchbay import DEFAULT_PATCHBAY, PatchCable, SinkType

    test_cable = PatchCable(
        name="test-dispatch-cable",
        sink_type=SinkType.LOCAL_LOG,
        destination="audit-logger",
    )
    DEFAULT_PATCHBAY.connect(test_cable)

    try:
        r = client.post("/v1/dispatch", json={
            "message_id": "disp-001",
            "channel_id": "sms",
            "sender": "+18005551234",
            "subject": "Wire transfer update",
        })
        assert r.status_code == 200
        data = r.json()
        assert data["cables_triggered"] >= 1
        assert data["action"]["message_id"] == "disp-001"
    finally:
        DEFAULT_PATCHBAY.disconnect("test-dispatch-cable")
