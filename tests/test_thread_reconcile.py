"""Tests for server-truth thread reconciliation — the fix for stale/triplicate drafts.

``IMAPProvider.thread_already_handled`` asks the Gmail server directly whether a
reply-owed thread is already handled: a reply in ``[Gmail]/Sent Mail`` (operator
already answered) or a draft in ``[Gmail]/Drafts`` (dedup). If either exists the
draft leaf skips it, so the beat never re-drafts an answered thread nor piles up
duplicates.

The FakeConn stub records SELECT/SEARCH and has NO send method — so a test that
accidentally tried to send would AttributeError, proving reconciliation only reads.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers.imap import IMAPProvider  # noqa: E402


class FakeConn:
    """Minimal imaplib.IMAP4_SSL stand-in: SELECT (readonly) + UID SEARCH only."""

    def __init__(self, sent_hits=(), drafts_hits=(), search_status="OK"):
        self._hits = {"[Gmail]/Sent Mail": sent_hits, "[Gmail]/Drafts": drafts_hits}
        self._status = search_status
        self.selected = None
        self.searches = []          # (mailbox, args) recorded
        self.readonly_selects = []  # mailboxes selected read-only

    def select(self, mailbox, readonly=False):
        self.selected = mailbox
        if readonly:
            self.readonly_selects.append(mailbox)
        return ("OK", [b"1"])

    def uid(self, command, *args):
        if command.lower() == "search":
            self.searches.append((self.selected, args))
            hits = self._hits.get(self.selected, ())
            data = b" ".join(hits) if hits else b""
            return (self._status, [data])
        raise AssertionError(f"unexpected uid command in reconcile: {command!r}")

    # Deliberately NO login/send/append/sendmail — reconciliation must only read.


def _provider(conn):
    prov = IMAPProvider(user="me@gmail.com", password="x", use_gmail_extensions=True)  # allow-secret
    prov._connection = conn  # inject the fake; bypass connect()/login
    return prov


def test_norm_subject_strips_reply_prefixes():
    assert IMAPProvider._norm_subject("Re: Foo") == "Foo"
    assert IMAPProvider._norm_subject("RE: FWD: Foo Bar") == "Foo Bar"
    assert IMAPProvider._norm_subject("  Fw:  Foo ") == "Foo"
    assert IMAPProvider._norm_subject("Plain") == "Plain"


def test_handled_true_when_reply_in_sent():
    conn = FakeConn(sent_hits=(b"42",))
    prov = _provider(conn)
    assert prov.thread_already_handled("a@b.com", "Re: Motion") is True
    # Sent is searched first; a hit there short-circuits (Drafts not needed).
    assert conn.searches[0][0] == "[Gmail]/Sent Mail"
    # searched by the Re:-stripped stem, quoted
    assert '"Motion"' in conn.searches[0][1]


def test_handled_true_when_draft_in_drafts_only():
    conn = FakeConn(sent_hits=(), drafts_hits=(b"7",))
    prov = _provider(conn)
    assert prov.thread_already_handled("a@b.com", "Motion") is True
    searched = [m for (m, _a) in conn.searches]
    assert "[Gmail]/Sent Mail" in searched and "[Gmail]/Drafts" in searched


def test_not_handled_when_neither():
    conn = FakeConn(sent_hits=(), drafts_hits=())
    prov = _provider(conn)
    assert prov.thread_already_handled("a@b.com", "Motion") is False


def test_fail_open_on_search_error_returns_not_handled():
    # A non-OK SEARCH must read as "not handled" so a real draft is never suppressed.
    conn = FakeConn(sent_hits=(b"1",), drafts_hits=(b"1",), search_status="NO")
    prov = _provider(conn)
    assert prov.thread_already_handled("a@b.com", "Motion") is False


def test_empty_subject_or_addr_is_not_handled():
    conn = FakeConn(sent_hits=(b"1",))
    prov = _provider(conn)
    assert prov.thread_already_handled("", "Motion") is False
    assert prov.thread_already_handled("a@b.com", "") is False
    assert conn.searches == []  # no server round-trip when nothing to match


def test_reconcile_selects_read_only_never_mutates():
    conn = FakeConn(drafts_hits=(b"9",))
    prov = _provider(conn)
    prov.thread_already_handled("a@b.com", "Motion")
    # every SELECT during reconciliation was read-only (no mailbox mutation)
    assert conn.readonly_selects == [m for (m, _a) in conn.searches]
