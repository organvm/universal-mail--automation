"""Tests for IMAPProvider.archive / remove_label disposition honesty.

Reviews U086 (gmail-ext archive reported success even when the \\Inbox-label
STORE was rejected) and U131 (standard-IMAP archive did COPY + STORE \\Deleted
then returned True WITHOUT expunge — leaving the message in the inbox flagged
\\Deleted, which clients hide, while the caller recorded did_leave_inbox=True).

Invariants enforced here:
  * archive() reports True ONLY when the message actually left the source mailbox.
  * archive() NEVER issues a mailbox-wide expunge() (would destroy unrelated mail).
  * every STORE/COPY/MOVE/EXPUNGE return code is honoured (no false success).
"""

import imaplib

import pytest

from providers.imap import IMAPProvider


class FakeConn:
    """Records every UID command; trips ``bare_expunge_called`` on the
    mailbox-wide ``expunge()`` that must never be issued."""

    def __init__(self, capabilities=(), results=None, raise_on=()):
        self.capabilities = tuple(c.upper() for c in capabilities)
        self.calls = []
        self.bare_expunge_called = False
        self._results = results or {}
        self._raise_on = {c.upper() for c in raise_on}

    def uid(self, command, *args):
        cmd = command.upper()
        self.calls.append((cmd, *args))
        if cmd in self._raise_on:
            raise imaplib.IMAP4.error(cmd)
        return (self._results.get(cmd, "OK"), [None])

    def expunge(self):
        self.bare_expunge_called = True
        return ("OK", [b"1"])

    def cmds(self):
        return [c[0] for c in self.calls]


def _provider(gmail_ext=False, conn=None, mailbox="INBOX"):
    p = IMAPProvider(host="imap.example.com", user="u@example.com",
                     password="x", use_gmail_extensions=gmail_ext)  # allow-secret: test literal
    p._connection = conn
    p._current_mailbox = mailbox
    return p


# -- the load-bearing invariant: never mailbox-wide expunge -----------------
@pytest.mark.parametrize("caps", [(), ("UIDPLUS",), ("MOVE",), ("MOVE", "UIDPLUS")])
def test_standard_archive_never_calls_mailbox_wide_expunge(caps):
    conn = FakeConn(capabilities=caps)
    _provider(conn=conn).archive("42")
    assert conn.bare_expunge_called is False


# -- gmail-extensions archive: \Deleted + scoped UID EXPUNGE -----------------
# -X-GM-LABELS \Inbox is a SILENT NO-OP on Gmail (verified live 2026-07-03:
# store OK, message still in inbox — Gmail doesn't expose \Inbox as a label).
# The real primitive is +FLAGS \Deleted + a UID-scoped EXPUNGE while INBOX is
# selected; the message leaves the inbox but survives in All Mail.
def test_gmail_archive_uses_deleted_plus_scoped_expunge():
    conn = FakeConn(capabilities=("UIDPLUS",))
    assert _provider(gmail_ext=True, conn=conn).archive("1") is True
    assert conn.cmds() == ["STORE", "EXPUNGE"]
    assert ("STORE", "1", "+FLAGS", r"(\Deleted)") in conn.calls
    assert ("EXPUNGE", "1") in conn.calls        # scoped to the uid
    assert conn.bare_expunge_called is False


def test_gmail_archive_false_and_no_expunge_when_delete_flag_rejected():
    conn = FakeConn(capabilities=("UIDPLUS",), results={"STORE": "NO"})
    assert _provider(gmail_ext=True, conn=conn).archive("1") is False
    assert conn.cmds() == ["STORE"]              # never reached EXPUNGE


def test_gmail_archive_refuses_outside_inbox():
    # From [Gmail]/All Mail, \Deleted+EXPUNGE would DELETE — must refuse, no writes.
    conn = FakeConn(capabilities=("UIDPLUS",))
    p = _provider(gmail_ext=True, conn=conn, mailbox="[Gmail]/All Mail")
    assert p.archive("1") is False
    assert conn.calls == []                      # no STORE, no EXPUNGE
    assert conn.bare_expunge_called is False


def test_gmail_archive_refuses_without_uidplus():
    # No UIDPLUS -> cannot scope the EXPUNGE -> refuse rather than risk a
    # mailbox-wide expunge of every \Deleted message.
    conn = FakeConn(capabilities=())
    assert _provider(gmail_ext=True, conn=conn).archive("1") is False
    assert conn.calls == []


def test_gmail_archive_rolls_back_deleted_flag_on_expunge_failure():
    # \Deleted set but EXPUNGE rejected -> must clear \Deleted so the message is
    # never left hidden-but-present in the inbox.
    conn = FakeConn(capabilities=("UIDPLUS",), results={"EXPUNGE": "NO"})
    assert _provider(gmail_ext=True, conn=conn).archive("1") is False
    assert ("STORE", "1", "+FLAGS", r"(\Deleted)") in conn.calls
    assert ("STORE", "1", "-FLAGS", r"(\Deleted)") in conn.calls   # rollback
    assert conn.bare_expunge_called is False


def test_gmail_remove_label_checks_store_result():
    conn = FakeConn(results={"STORE": "NO"})
    assert _provider(gmail_ext=True, conn=conn).remove_label("1", "Work") is False
    conn_ok = FakeConn()
    assert _provider(gmail_ext=True, conn=conn_ok).remove_label("1", "Work") is True


# -- standard IMAP: atomic MOVE (RFC 6851) ----------------------------------
def test_standard_archive_uses_move_when_supported():
    conn = FakeConn(capabilities=("MOVE", "UIDPLUS"))
    assert _provider(conn=conn).archive("7") is True
    assert conn.cmds() == ["MOVE"]             # no COPY/STORE/EXPUNGE


def test_standard_archive_move_non_ok_is_false_no_copy_fallthrough():
    conn = FakeConn(capabilities=("MOVE",), results={"MOVE": "NO"})
    assert _provider(conn=conn).archive("7") is False
    assert conn.cmds() == ["MOVE"]             # crucially no COPY (no duplicate)


# -- standard IMAP: COPY + scoped UID EXPUNGE (UIDPLUS) ----------------------
def test_standard_archive_copy_store_scoped_expunge_when_uidplus():
    conn = FakeConn(capabilities=("UIDPLUS",))
    assert _provider(conn=conn).archive("9") is True
    assert conn.cmds() == ["COPY", "STORE", "EXPUNGE"]
    assert ("EXPUNGE", "9") in conn.calls       # scoped to the uid
    assert conn.bare_expunge_called is False


def test_standard_archive_copy_failure_is_false_and_no_store():
    conn = FakeConn(capabilities=("UIDPLUS",), results={"COPY": "NO"})
    assert _provider(conn=conn).archive("9") is False
    assert conn.cmds() == ["COPY"]


def test_standard_archive_store_failure_is_false_and_no_expunge():
    conn = FakeConn(capabilities=("UIDPLUS",), results={"STORE": "NO"})
    assert _provider(conn=conn).archive("9") is False
    assert conn.cmds() == ["COPY", "STORE"]
    assert conn.bare_expunge_called is False


def test_standard_archive_scoped_expunge_failure_is_false():
    conn = FakeConn(capabilities=("UIDPLUS",), results={"EXPUNGE": "NO"})
    assert _provider(conn=conn).archive("9") is False


# -- standard IMAP: neither MOVE nor UIDPLUS -> honest failure, no mutation --
def test_standard_archive_no_move_no_uidplus_reports_false_without_mutating():
    # The core U131 regression: the old code did COPY + STORE \Deleted then
    # returned True without expunge, leaving the message in the inbox while the
    # caller recorded archived=True. Now: no removal primitive -> no changes,
    # honest False.
    conn = FakeConn(capabilities=())
    assert _provider(conn=conn).archive("9") is False
    assert conn.calls == []                     # NO copy, NO \Deleted flag set
    assert conn.bare_expunge_called is False


# -- exceptions are caught and reported as failure --------------------------
def test_standard_archive_exception_is_false():
    conn = FakeConn(capabilities=("MOVE",), raise_on=("MOVE",))
    assert _provider(conn=conn).archive("9") is False


# -- U085: every flag/label STORE honours the server's NO -------------------
# imaplib raises only on BAD; a server NO (quota, ACL, read-only mailbox,
# invalid flag) is a normal ('NO', ...) tuple. These methods used to return
# True unconditionally, so rejections entered the audit as applied.
_STORE_METHODS = [
    # (method-name, args, gmail_ext)
    ("apply_label", ("1", "Work"), True),
    ("remove_label", ("1", "Work"), True),
    ("star", ("1",), False),
    ("unstar", ("1",), False),
    ("mark_read", ("1",), False),
    ("mark_unread", ("1",), False),
]


@pytest.mark.parametrize("method,args,gmail_ext", _STORE_METHODS)
def test_store_method_false_when_server_says_no(method, args, gmail_ext):
    conn = FakeConn(results={"STORE": "NO"})
    assert getattr(_provider(gmail_ext=gmail_ext, conn=conn), method)(*args) is False


@pytest.mark.parametrize("method,args,gmail_ext", _STORE_METHODS)
def test_store_method_true_when_server_says_ok(method, args, gmail_ext):
    conn = FakeConn()
    assert getattr(_provider(gmail_ext=gmail_ext, conn=conn), method)(*args) is True
    assert conn.cmds() == ["STORE"]


@pytest.mark.parametrize("method,args,gmail_ext", _STORE_METHODS)
def test_store_method_false_on_exception(method, args, gmail_ext):
    conn = FakeConn(raise_on=("STORE",))
    assert getattr(_provider(gmail_ext=gmail_ext, conn=conn), method)(*args) is False


def test_store_flag_payloads_are_correct():
    # The refactor through _checked_store must not alter the wire arguments.
    cases = [
        ("star", ("7",), ("STORE", "7", "+FLAGS", r"(\Flagged)")),
        ("unstar", ("7",), ("STORE", "7", "-FLAGS", r"(\Flagged)")),
        ("mark_read", ("7",), ("STORE", "7", "+FLAGS", r"(\Seen)")),
        ("mark_unread", ("7",), ("STORE", "7", "-FLAGS", r"(\Seen)")),
    ]
    for method, args, expected in cases:
        conn = FakeConn()
        getattr(_provider(conn=conn), method)(*args)
        assert conn.calls == [expected], method


def test_gmail_label_store_payloads_are_correct():
    # USER labels are quoted, inside the parenthesised X-GM-LABELS list.
    conn = FakeConn()
    p = _provider(gmail_ext=True, conn=conn)
    p.apply_label("7", "Work/Dev")
    p.remove_label("7", "Work/Dev")
    assert conn.calls == [
        ("STORE", "7", "+X-GM-LABELS", '("Work/Dev")'),
        ("STORE", "7", "-X-GM-LABELS", '("Work/Dev")'),
    ]


def test_gmail_archive_never_touches_x_gm_labels():
    # Regression (verified live 2026-07-03): archive must NOT go through
    # -X-GM-LABELS \Inbox — that is a silent no-op on Gmail (\Inbox isn't a
    # label). It must use \Deleted + scoped EXPUNGE. Guard that no X-GM-LABELS
    # command is ever issued by archive().
    conn = FakeConn(capabilities=("UIDPLUS",))
    _provider(gmail_ext=True, conn=conn).archive("7")
    assert not any("X-GM-LABELS" in str(c) for c in conn.calls)


def test_gm_label_value_forms():
    assert IMAPProvider._gm_label_value("\\Inbox") == r"(\Inbox)"
    assert IMAPProvider._gm_label_value("\\Starred") == r"(\Starred)"
    assert IMAPProvider._gm_label_value("Work/Dev") == '("Work/Dev")'
    # a user label containing a quote is escaped, still quoted
    assert IMAPProvider._gm_label_value('a"b') == '("a\\"b")'
