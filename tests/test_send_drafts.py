"""Fail-closed guarantees of the tiered auto-sender (send_drafts.py).

These lock in the three independent send-locks and, critically, that a legal/money HOLD can
never be reclassified out by an explicit safe opt-in. If any of these regress the sender could
send something it must not — so they are load-bearing, not smoke tests.
"""

import json
import os

import send_drafts

# A minimal registry mirroring institutio/governance/mail-tiers.yaml's shape.
TIERS = {
    "no_reply": {
        "esp_domains": ["ccsend.com", "e.*", "email.*"],
        "role_localparts": ["noreply", "newsletter", "marketing"],
        "subject_patterns": [r"(?i)\bweekly\b", r"(?i)\bdigest\b"],
        "header_signals": ["List-Unsubscribe"],
    },
    "hold": {
        "classes": ["legal-correspondence", "legal-sign"],
        "tags": ["legal", "money", "security"],
        "verify_first": True,
    },
    "safe": {
        "armed_by": "LIMEN_MAIL_SEND",
        "intents": [{"id": "decline", "when": "cold pitch", "template": "Hi {first_name},\n\nNo thanks.\n\nBest,\nAnthony"}],
    },
}


def _ob(**kw):
    base = {"requires_reply": True, "cls": "precedent", "tags": ["human"],
            "sender": "Real Person <person@company.com>", "sample_subjects": ["Hello"], "message_ids": ["m1"]}
    base.update(kw)
    return base


def test_no_reply_suppresses_esp_and_role():
    assert send_drafts.is_no_reply(_ob(sender="News <newsletter@e.brand.com>"), TIERS)  # esp wildcard
    assert send_drafts.is_no_reply(_ob(sender="X <noreply@company.com>"), TIERS)        # role localpart
    assert send_drafts.is_no_reply(_ob(sample_subjects=["Our Weekly roundup"]), TIERS)  # subject pattern
    assert not send_drafts.is_no_reply(_ob(), TIERS)                                     # a real person


def test_legal_holds_even_with_safe_optin():
    ob = _ob(cls="legal-correspondence", tags=["legal"], safe_intent="decline")
    assert send_drafts.tier_of(ob, TIERS) == "hold"       # hold is checked FIRST


def test_money_tag_holds():
    assert send_drafts.tier_of(_ob(tags=["money"], safe_intent="decline"), TIERS) == "hold"


def test_safe_requires_explicit_optin():
    assert send_drafts.tier_of(_ob(), TIERS) == "hold"                       # no opt-in ⇒ hold (fail-closed)
    assert send_drafts.tier_of(_ob(safe_intent="decline"), TIERS) == "safe"  # opt-in ⇒ safe
    assert send_drafts.tier_of(_ob(safe_intent="not-a-real-intent"), TIERS) == "hold"  # unknown id ⇒ hold


def test_render_safe_is_bracket_free():
    text = send_drafts.render_safe(_ob(sender="Bob Smith <bob@x.com>"), TIERS, "decline")
    assert "Bob" in text and "[" not in text and "{" not in text


def test_disarmed_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("LIMEN_MAIL_SEND", raising=False)
    ledger = tmp_path / "led.json"
    ledger.write_text(json.dumps({"obligations": [_ob(safe_intent="decline", sender="Bob <bob@acme.com>")]}))
    monkeypatch.setattr(send_drafts, "load_tiers", lambda: TIERS)
    monkeypatch.setattr(send_drafts, "_SENT_STATE", str(tmp_path / "sent.json"))
    assert send_drafts.main(["--ledger", str(ledger)]) == 0
    assert not os.path.exists(str(tmp_path / "sent.json"))  # nothing sent ⇒ no audit rows written


def test_armed_without_credentials_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMEN_MAIL_SEND", "1")
    for k in ("IMAP_USER", "IMAP_PASS", "GMAIL_USER", "GMAIL_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    ledger = tmp_path / "led.json"
    ledger.write_text(json.dumps({"obligations": [_ob(safe_intent="decline", sender="Bob <bob@acme.com>")]}))
    monkeypatch.setattr(send_drafts, "load_tiers", lambda: TIERS)
    monkeypatch.setattr(send_drafts, "_SENT_STATE", str(tmp_path / "sent.json"))
    # A send would raise if attempted (no creds); main must fail closed and never call SMTP.
    monkeypatch.setattr(send_drafts, "send_reply", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not send")))
    assert send_drafts.main(["--ledger", str(ledger)]) == 0
    assert not os.path.exists(str(tmp_path / "sent.json"))
