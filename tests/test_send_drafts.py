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


# ── Keyed single-fire: the explicit "send THIS one" button (mode-gated, attachment-capable) ──
# These lock in the switchable HOLD-send boundary + attachments, and — load-bearing — that a
# HOLD/ad-hoc target is NEVER transmitted under the fail-closed default mode.

def _throw_if_sent(*_a, **_k):
    raise AssertionError("send_reply must not be called")


class _FakeSMTP:
    """Captures the composed message instead of opening a real SMTP_SSL connection."""
    captured = None

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *a, **k):
        pass

    def send_message(self, msg):
        _FakeSMTP.captured = msg


def test_resolve_send_mode_default_override_and_unknown(monkeypatch):
    monkeypatch.delenv("LIMEN_MAIL_HOLD_SEND", raising=False)
    assert send_drafts.resolve_send_mode(None) == "safe_only"                                # fail-closed default
    assert send_drafts.resolve_send_mode({"send_mode": {"mode": "keyed_all"}}) == "keyed_all"  # registry value
    assert send_drafts.resolve_send_mode({"send_mode": {"mode": "bogus"}}) == "safe_only"      # unknown ⇒ default
    monkeypatch.setenv("LIMEN_MAIL_HOLD_SEND", "per_matter")
    assert send_drafts.resolve_send_mode({"send_mode": {"mode": "keyed_all"}}) == "per_matter"  # env overrides


def test_mode_permits_keyed_matrix():
    assert send_drafts.mode_permits_keyed("safe_only", "safe", {})           # SAFE always sendable
    assert not send_drafts.mode_permits_keyed("safe_only", "hold", {})       # HOLD refused by default
    assert send_drafts.mode_permits_keyed("keyed_all", "hold", {})           # keyed_all sends any tier
    assert not send_drafts.mode_permits_keyed("per_matter", "hold", {})      # per_matter needs opt-in
    assert send_drafts.mode_permits_keyed("per_matter", "hold", {"send_ok": True})


def test_classify_attachments_splits_ok_oversized_missing(tmp_path):
    good = tmp_path / "a.txt"
    good.write_bytes(b"hi")
    big = tmp_path / "b.bin"
    big.write_bytes(b"x" * 100)
    ok, oversized, missing = send_drafts.classify_attachments(
        [str(good), str(big), str(tmp_path / "nope.pdf")], max_bytes=10)
    assert [p.name for p in ok] == ["a.txt"]
    assert oversized == [str(big)]
    assert missing == [str(tmp_path / "nope.pdf")]


def test_send_reply_attaches_pdf(tmp_path, monkeypatch):
    pdf = tmp_path / "brief.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(send_drafts.smtplib, "SMTP_SSL", _FakeSMTP)
    assert send_drafts.send_reply("bob@x.com", "Hi", "See attached.", ("u@gmail.com", "pw"), attachments=[pdf])
    atts = list(_FakeSMTP.captured.iter_attachments())
    assert len(atts) == 1
    assert atts[0].get_filename() == "brief.pdf"
    assert atts[0].get_content() == b"%PDF-1.4 fake"


def test_fire_hold_refused_in_safe_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMEN_MAIL_HOLD_SEND", "safe_only")
    monkeypatch.setattr(send_drafts, "load_tiers", lambda: TIERS)
    monkeypatch.setattr(send_drafts, "_SENT_STATE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(send_drafts, "_smtp_creds", lambda: ("u@gmail.com", "pw"))
    monkeypatch.setattr(send_drafts, "send_reply", _throw_if_sent)  # an ad-hoc target is HOLD ⇒ must refuse
    assert send_drafts.main(["--fire-to", "bob@acme.com", "--fire-subject", "Hi",
                             "--fire-body", "Confirming.", "--fire"]) == 0
    assert not os.path.exists(str(tmp_path / "sent.json"))


def test_fire_hold_sent_in_keyed_all_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMEN_MAIL_HOLD_SEND", "keyed_all")
    monkeypatch.setattr(send_drafts, "load_tiers", lambda: TIERS)
    monkeypatch.setattr(send_drafts, "_SENT_STATE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(send_drafts, "_smtp_creds", lambda: ("u@gmail.com", "pw"))
    calls = []
    monkeypatch.setattr(send_drafts, "send_reply",
                        lambda to, subj, body, creds, attachments=None: calls.append(to) or True)
    argv = ["--fire-to", "bob@acme.com", "--fire-subject", "Hi", "--fire-body", "Confirming.", "--fire"]
    assert send_drafts.main(argv) == 0
    assert calls == ["bob@acme.com"]
    assert send_drafts.main(argv) == 0        # a second identical fire must NOT re-transmit
    assert calls == ["bob@acme.com"]


def test_fire_oversized_attachment_refused(tmp_path, monkeypatch):
    big = tmp_path / "huge.pdf"
    big.write_bytes(b"x" * 64)
    monkeypatch.setattr(send_drafts, "_MAX_INLINE_BYTES", 8)   # 8-byte ceiling for the test
    monkeypatch.setenv("LIMEN_MAIL_HOLD_SEND", "keyed_all")
    monkeypatch.setattr(send_drafts, "load_tiers", lambda: TIERS)
    monkeypatch.setattr(send_drafts, "_SENT_STATE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(send_drafts, "_smtp_creds", lambda: ("u", "p"))
    monkeypatch.setattr(send_drafts, "send_reply", _throw_if_sent)  # oversized ⇒ refuse whole send
    assert send_drafts.main(["--fire-to", "bob@acme.com", "--fire-subject", "Hi",
                             "--fire-body", "See attached.", "--attach", str(big), "--fire"]) == 0
    assert not os.path.exists(str(tmp_path / "sent.json"))


def test_fire_dry_run_transmits_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMEN_MAIL_HOLD_SEND", "keyed_all")
    monkeypatch.delenv("LIMEN_MAIL_SEND", raising=False)
    monkeypatch.setattr(send_drafts, "load_tiers", lambda: TIERS)
    monkeypatch.setattr(send_drafts, "_SENT_STATE", str(tmp_path / "sent.json"))
    monkeypatch.setattr(send_drafts, "send_reply", _throw_if_sent)  # no --fire ⇒ dry-run, never sends
    assert send_drafts.main(["--fire-to", "bob@acme.com", "--fire-subject", "Hi", "--fire-body", "Hello."]) == 0
    assert not os.path.exists(str(tmp_path / "sent.json"))
