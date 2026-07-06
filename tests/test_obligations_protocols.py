"""Tests for obligations protocol classification.

Samples are synthetic. They mirror recurring-payment shapes without embedding private mail.
"""

import json

from inbox_sweep import decide
from core.protocols import derive
from obligations_build import build


def test_subscription_renewal_can_match_snippet_only():
    ob = derive(
        "Example Service <no-reply@example.com>",
        "Example+: Paid Membership Confirmation",
        snippet="On August 6, 2026, your membership will automatically renew "
        "for a full month and you will be charged the $4.99 membership fee.",
    )

    assert ob.cls == "subscription-renewal"
    assert ob.priority == 54
    assert ob.tags == ["money", "subscription"]


def test_paid_membership_confirmation_matches_subject_only():
    ob = derive(
        "Example Service <no-reply@example.com>",
        "Example+: Paid Membership Confirmation",
    )

    assert ob.cls == "subscription-renewal"


def test_paid_membership_confirmation_surfaces_as_fire():
    assert (
        decide(
            "Example Service <no-reply@example.com>",
            "Example+: Paid Membership Confirmation",
            2,
            False,
            "Finance/Payments",
        )
        == "fire"
    )


def test_obligations_builder_passes_snippet_to_protocols(tmp_path):
    receipt = {
        "result": {"account": "example-account", "total": 1},
        "rows": [
            {
                "id": "m1",
                "action": "fire",
                "sender": "Example Service <no-reply@example.com>",
                "subject": "Example+: Paid Membership Confirmation",
                "snippet": "Your membership will automatically renew and you will be charged.",
                "label": "Finance/Payments",
                "tier": 2,
            }
        ],
    }
    (tmp_path / "inbox_sweep-example.json").write_text(json.dumps(receipt), encoding="utf-8")

    ledger = build(str(tmp_path))

    assert ledger["totals"]["by_class"]["subscription-renewal"] == 1
    assert ledger["obligations"][0]["title"] == "Subscription renewal — Example Service"
