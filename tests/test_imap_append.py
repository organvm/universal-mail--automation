"""Tests for IMAPProvider.append / create_draft — the keyed, headless draft-save
path that designs out the macOS TCC Automation grant (lever L-MAIL-AUTOMATION-GRANT
#960). This is the reversible counterpart to MailAppProvider.create_draft: it writes a
DRAFT to [Gmail]/Drafts via IMAP APPEND and NEVER sends.

Invariants enforced here:
  * append() issues exactly one APPEND to [Gmail]/Drafts with the \\Draft flag.
  * append() reports True ONLY on an OK response (a server NO / raised BAD -> False),
    mirroring the _checked_store honesty precedent (reviews U085/U131).
  * create_draft() builds an RFC822 reply (From = the authenticated mailbox), adds a
    single Re: prefix, and routes through append() — so it, too, can never send.
"""

import imaplib

from providers.imap import IMAPProvider


class FakeConn:
    """Records every APPEND; trips ``sent`` only if a send were ever attempted
    (it must not be — APPEND is the sole write primitive here)."""

    def __init__(self, results=None, raise_on_append=False):
        self.appends = []
        self.sent = False
        self._results = results or {}
        self._raise = raise_on_append

    def append(self, mailbox, flags, date_time, message):
        self.appends.append((mailbox, flags, date_time, message))
        if self._raise:
            raise imaplib.IMAP4.error("APPEND")
        return (self._results.get("APPEND", "OK"), [None])

    # present so a stray send in the code under test would blow up loudly
    def send(self, *a, **k):  # pragma: no cover - must never be called
        self.sent = True
        raise AssertionError("append/create_draft must never send")


def _provider(conn):
    p = IMAPProvider(host="imap.gmail.com", user="u@gmail.com",
                     password="x", use_gmail_extensions=True)  # allow-secret: test literal
    p._connection = conn  # preset so connect() is a no-op (no real login)
    return p


# -- append(): the wire shape -----------------------------------------------
def test_append_writes_draft_flag_to_gmail_drafts():
    conn = FakeConn()
    assert _provider(conn).append(b"raw-bytes") is True
    assert len(conn.appends) == 1
    mailbox, flags, date_time, message = conn.appends[0]
    assert mailbox == "[Gmail]/Drafts"
    assert flags == r"(\Draft)"
    assert date_time is None
    assert message == b"raw-bytes"


def test_append_encodes_str_payload():
    conn = FakeConn()
    _provider(conn).append("héllo")  # str -> utf-8 bytes
    _mb, _fl, _dt, message = conn.appends[0]
    assert isinstance(message, bytes)
    assert message == "héllo".encode("utf-8")


def test_append_custom_mailbox():
    conn = FakeConn()
    _provider(conn).append(b"x", mailbox="Drafts")
    assert conn.appends[0][0] == "Drafts"


# -- append(): fail-closed honesty ------------------------------------------
def test_append_false_on_server_no():
    conn = FakeConn(results={"APPEND": "NO"})
    assert _provider(conn).append(b"x") is False


def test_append_false_on_exception():
    conn = FakeConn(raise_on_append=True)
    assert _provider(conn).append(b"x") is False


# -- create_draft(): builds a reply MIME and APPENDs it ---------------------
def test_create_draft_builds_reply_mime_and_appends():
    conn = FakeConn()
    assert _provider(conn).create_draft(
        "boss@corp.com", "Quarterly plan", "Sounds good.\n") is True
    mailbox, flags, _dt, message = conn.appends[0]
    assert mailbox == "[Gmail]/Drafts"
    assert flags == r"(\Draft)"
    text = message.decode("utf-8")
    assert "To: boss@corp.com" in text
    assert "From: u@gmail.com" in text          # the authenticated mailbox, not the account name
    assert "Subject: Re: Quarterly plan" in text  # Re: prefix added once
    assert "Sounds good." in text


def test_create_draft_preserves_existing_re_prefix():
    conn = FakeConn()
    _provider(conn).create_draft("x@y.com", "Re: already open", "body")
    text = conn.appends[0][3].decode("utf-8")
    assert text.count("Subject: Re:") == 1
    assert "Re: Re:" not in text


def test_create_draft_ignores_account_name_for_from():
    # `account` is an Apple-Mail account NAME (signature parity) — the keyed path always
    # writes as the logged-in Gmail mailbox, never as the account-name string.
    conn = FakeConn()
    _provider(conn).create_draft("x@y.com", "s", "b", account="iCloud")
    text = conn.appends[0][3].decode("utf-8")
    assert "From: u@gmail.com" in text
    assert "iCloud" not in text


def test_create_draft_never_sends():
    conn = FakeConn()
    _provider(conn).create_draft("x@y.com", "s", "b")
    assert conn.sent is False          # no send path was taken
    assert len(conn.appends) == 1      # only a mailbox write happened
