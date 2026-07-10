"""Tests for obligations protocol classification.

Samples are synthetic. They mirror recurring-payment shapes without embedding private mail.
"""

import json

from inbox_sweep import decide
from core.protocols import derive, is_bulk_mail
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


# --- Bulk-header suppression (root-cause fix for the newsletter/transactional storm) ---

# Real, personal correspondents with NO bulk headers — these MUST stay reply-owed. (Real
# personal From lines carry a full display name; the bulk gate must never touch them.)
REAL_SENDERS = [
    ("Micah Longo <micahlongo@gmail.com>", "Padavano v. MDC (Depositions)"),
    ("Zafer Ramzan <zafer@algora.io>", "Air Space Intelligence interview"),
    ("Uruba Niazi <uruba.niazi@authplane.ai>", "quick question on FastMCP + auth"),
]


def test_real_personal_senders_stay_reply_owed_without_headers():
    for sender, subject in REAL_SENDERS:
        ob = derive(sender, subject)
        assert ob.requires_reply is True, sender
        assert ob.rung == "precedent", sender
        assert ob.cls != "bulk", sender


def test_real_personal_senders_stay_reply_owed_even_with_personal_headers():
    # A normal personal message may carry ordinary headers but NONE of the bulk markers.
    personal_headers = "From: Micah Longo <micahlongo@gmail.com>\r\nMessage-ID: <abc@mail>"
    ob = derive(*REAL_SENDERS[0], headers=personal_headers)
    assert ob.requires_reply is True
    assert ob.rung == "precedent"


def test_list_unsubscribe_header_suppresses_precedent():
    # Socket "Socket Weekly" newsletter fronting a real human name — used to over-fire.
    ob = derive(
        "Feross Aboukhadijeh <feross@socket.dev>",
        "Socket Weekly",
        headers="List-Unsubscribe: <https://socket.dev/unsub>\r\n"
                "List-Unsubscribe-Post: List-Unsubscribe=One-Click",
    )
    assert ob.cls == "bulk"
    assert ob.rung == "bulk"
    assert ob.requires_reply is False


def test_list_id_header_suppresses_precedent():
    ob = derive(
        "Hello Developer <developer@insideapple.apple.com>",
        "Hello Developer",
        headers={"List-Id": "Hello Developer <hello.apple.com>"},
    )
    assert ob.cls == "bulk"
    assert ob.requires_reply is False


def test_precedence_bulk_header_suppresses_precedent():
    ob = derive(
        "Laughing Buddha Comedy <laughingbuddhacomedy@buytickets.at>",
        "Your ticket confirmation",
        headers="Precedence: bulk",
    )
    assert ob.cls == "bulk"
    assert ob.requires_reply is False


def test_is_bulk_mail_header_detection():
    assert is_bulk_mail("List-Unsubscribe: <mailto:u@x>") is True
    assert is_bulk_mail({"List-Id": "x"}) is True
    assert is_bulk_mail("Precedence: list") is True
    assert is_bulk_mail("Precedence: auto_reply") is True
    assert is_bulk_mail("Auto-Submitted: auto-generated") is True
    # Personal / absent → not bulk (fail-open).
    assert is_bulk_mail("") is False
    assert is_bulk_mail(None) is False
    assert is_bulk_mail("Auto-Submitted: no") is False
    assert is_bulk_mail("From: a@b\r\nSubject: hi") is False


def test_folded_header_continuation_line_is_parsed():
    # RFC 5322 folded header: value continues on an indented line.
    raw = "List-Unsubscribe: <https://x/unsub>,\r\n <mailto:unsub@x>"
    assert is_bulk_mail(raw) is True


def test_builder_suppresses_bulk_headered_row(tmp_path):
    receipt = {
        "result": {"account": "acct", "total": 2},
        "rows": [
            {   # newsletter with bulk header -> suppressed, not reply-owed
                "id": "n1", "action": "fire",
                "sender": "Feross Aboukhadijeh <feross@socket.dev>",
                "subject": "Socket Weekly", "tier": 4,
                "headers": "List-Unsubscribe: <https://socket.dev/unsub>",
            },
            {   # real person, no bulk header -> stays reply-owed
                "id": "r1", "action": "fire",
                "sender": "Uruba Niazi <uruba.niazi@authplane.ai>",
                "subject": "quick question on FastMCP + auth", "tier": 4,
            },
        ],
    }
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(receipt), encoding="utf-8")

    ledger = build(str(tmp_path))
    by_class = ledger["totals"]["by_class"]
    assert by_class.get("bulk") == 1
    assert by_class.get("precedent") == 1
    reply_owed = [o for o in ledger["obligations"] if o["requires_reply"]]
    assert [o["domain"] for o in reply_owed] == ["authplane.ai"]
