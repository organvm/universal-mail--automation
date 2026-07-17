"""mail_send.py — the interactive send lane. Fail-closed + verification tests.

Conventions follow tests/test_send_drafts.py: a _FakeSMTP captures instead of
transmitting; GmailImap is faked at the injection seam (send_and_verify /
run_from_draft take the imap object), so no imaplib protocol emulation.
"""

from __future__ import annotations

import email
import email.policy

import pytest

import mail_send
from mail_send import (
    EXIT_FAIL_CLOSED,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_UNVERIFIED,
    build_message,
    run_from_draft,
    send_and_verify,
)

CREDS = ("me@example.com", "app-password")


class _FakeSMTP:
    """Captures the composed message + envelope instead of opening a real connection."""

    captured = None
    envelope = None
    logged_in = None

    def __init__(self, host, port, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        _FakeSMTP.logged_in = (user, pw)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        _FakeSMTP.captured = msg
        _FakeSMTP.envelope = (from_addr, to_addrs)


class _FakeImap:
    def __init__(self, sent_ok=True, draft_raw=None, match=None, trash_ok=True):
        self.sent_ok = sent_ok
        self.draft_raw = draft_raw
        self.match = match
        self.trash_ok = trash_ok
        self.trashed = []

    def newest_matching(self, mailbox, query):
        return self.match

    def fetch_raw(self, mailbox, uid):
        return self.draft_raw

    def sent_has(self, message_id, timeout_s=60, step_s=5):
        return self.sent_ok

    def trash_draft(self, uid):
        self.trashed.append(uid)
        return self.trash_ok

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _fake_smtp(monkeypatch):
    _FakeSMTP.captured = None
    _FakeSMTP.envelope = None
    _FakeSMTP.logged_in = None
    monkeypatch.setattr(mail_send.smtplib, "SMTP_SSL", _FakeSMTP)


def test_reply_threading_headers():
    reply_headers = {
        "Message-ID": "<orig-123@mail.example>",
        "References": "<root-1@mail.example>",
        "Subject": "Docket X — request",
        "From": "Clerk <clerk@example.gov>",
    }
    msg = build_message(CREDS, ["clerk@example.gov"], "", "body text", reply_headers=reply_headers)
    assert msg["In-Reply-To"] == "<orig-123@mail.example>"
    assert msg["References"] == "<root-1@mail.example> <orig-123@mail.example>"
    assert msg["Subject"] == "Re: Docket X — request"
    assert msg["Message-ID"]  # always set, so the send is verifiable


def test_reply_subject_unfolds_folded_header():
    # RFC 5322 folds long subjects (CRLF + WSP). A folded subject carried into the
    # reply's "Re: ..." must be unfolded, or EmailMessage raises on the embedded
    # newline — the live ResponsiveAds miss (2026-07-17).
    folded = "ResponsiveAds Ad Format & Template Product Management Lead -\r\n ResponsiveAds, Inc."
    msg = build_message(
        CREDS, ["matt@example.com"], "", "x", reply_headers={"Message-ID": "<m@x>", "Subject": folded}
    )
    assert msg["Subject"] == "Re: ResponsiveAds Ad Format & Template Product Management Lead - ResponsiveAds, Inc."


def test_reply_subject_not_double_prefixed():
    msg = build_message(
        CREDS, ["a@b.c"], "", "x", reply_headers={"Message-ID": "<m@x>", "Subject": "Re: already"}
    )
    assert msg["Subject"] == "Re: already"


def test_send_and_verify_ok_and_unverified():
    msg = build_message(CREDS, ["a@b.c"], "s", "b")
    assert send_and_verify(msg, CREDS, _FakeImap(sent_ok=True), verify_timeout=1) == EXIT_OK
    assert _FakeSMTP.captured is not None
    assert send_and_verify(msg, CREDS, _FakeImap(sent_ok=False), verify_timeout=1) == EXIT_UNVERIFIED


def test_bcc_stripped_from_wire_but_delivered():
    msg = build_message(CREDS, ["to@x.y"], "s", "b", bcc=["hidden@x.y"])
    rc = send_and_verify(msg, CREDS, _FakeImap(), verify_timeout=1, to_addrs=["to@x.y", "hidden@x.y"])
    assert rc == EXIT_OK
    assert "Bcc" not in _FakeSMTP.captured
    assert _FakeSMTP.envelope == (CREDS[0], ["to@x.y", "hidden@x.y"])


def test_from_draft_verbatim_send_and_trash():
    draft = email.message.EmailMessage()
    draft["From"] = CREDS[0]
    draft["To"] = "alice@example.com"
    draft["Cc"] = "bob@example.com"
    draft["Bcc"] = "carol@example.com"
    draft["Subject"] = "Re: the thing"
    draft["Message-ID"] = "<draft-1@mail.example>"
    draft.set_content("draft body")
    imap = _FakeImap(match=(b"7", {"Subject": "Re: the thing"}), draft_raw=draft.as_bytes())
    rc = run_from_draft("the thing", CREDS, imap, verify_timeout=1, dry_run=False)
    assert rc == EXIT_OK
    assert "Bcc" not in _FakeSMTP.captured  # wire copy scrubbed
    assert _FakeSMTP.envelope[1] == ["alice@example.com", "bob@example.com", "carol@example.com"]
    assert _FakeSMTP.captured["Message-ID"] == "<draft-1@mail.example>"  # verbatim, not re-minted
    assert imap.trashed == [b"7"]  # sent ⇒ the draft copy is cleaned up


def test_from_draft_not_found():
    assert run_from_draft("nope", CREDS, _FakeImap(match=None), 1, False) == EXIT_NOT_FOUND
    assert _FakeSMTP.captured is None


def test_from_draft_dry_run_transmits_nothing():
    draft = email.message.EmailMessage()
    draft["To"] = "alice@example.com"
    draft["Subject"] = "x"
    draft.set_content("b")
    imap = _FakeImap(match=(b"9", {}), draft_raw=draft.as_bytes())
    rc = run_from_draft("x", CREDS, imap, verify_timeout=1, dry_run=True)
    assert rc == EXIT_OK
    assert _FakeSMTP.captured is None
    assert imap.trashed == []  # dry-run never mutates


def test_missing_creds_fails_closed(monkeypatch):
    for var in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "IMAP_USER", "IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    rc = mail_send.main(["--self-test"])
    assert rc == EXIT_FAIL_CLOSED
    assert _FakeSMTP.captured is None


def test_recipient_validation_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_USER", CREDS[0])
    monkeypatch.setenv("GMAIL_APP_PASSWORD", CREDS[1])
    body = tmp_path / "b.txt"
    body.write_text("hello")
    rc = mail_send.main(["--to", "not-an-address", "--subject", "s", "--body-file", str(body), "--dry-run"])
    assert rc == EXIT_FAIL_CLOSED
    rc = mail_send.main(["--to", "x@privaterelay.appleid.com", "--subject", "s", "--body-file", str(body), "--dry-run"])
    assert rc == EXIT_FAIL_CLOSED
    assert _FakeSMTP.captured is None


def test_compose_dry_run_transmits_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_USER", CREDS[0])
    monkeypatch.setenv("GMAIL_APP_PASSWORD", CREDS[1])
    body = tmp_path / "b.txt"
    body.write_text("hello there")
    rc = mail_send.main(["--to", "a@b.c", "--subject", "s", "--body-file", str(body), "--dry-run"])
    assert rc == EXIT_OK
    assert _FakeSMTP.captured is None


def test_missing_attachment_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("GMAIL_USER", CREDS[0])
    monkeypatch.setenv("GMAIL_APP_PASSWORD", CREDS[1])
    body = tmp_path / "b.txt"
    body.write_text("hello")
    rc = mail_send.main(
        ["--to", "a@b.c", "--subject", "s", "--body-file", str(body), "--attach", "/nonexistent.pdf", "--dry-run"]
    )
    assert rc == EXIT_FAIL_CLOSED
