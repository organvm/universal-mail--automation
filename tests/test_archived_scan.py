"""Pure-core tests for archived_scan.py — the archived-but-unanswered detector.

No Mail.app, no provider, no I/O: the join logic (subject-stem match against the Sent index) is a
pure function, so an archived inbound with no reply in Sent surfaces, and one whose stem IS in Sent
does not. This is the "have we responded to everything?" check made independent of the inbox.
"""

from archived_scan import (
    _norm_subject,
    _pick,
    sent_stem_index,
    unanswered_archived,
    ARCHIVE_CANDIDATES,
    SENT_CANDIDATES,
)


def test_norm_subject_strips_reply_prefixes_and_lowercases():
    assert _norm_subject("Re: Fwd:  Quick Question ") == "quick question"
    assert _norm_subject("FW: FW: Deal") == "deal"
    assert _norm_subject("  Plain  Subject ") == "plain subject"
    assert _norm_subject("") == ""


def test_sent_stem_index_normalizes_and_drops_empty():
    idx = sent_stem_index(["Re: Quick Question", "", "  ", "Deal Terms"])
    assert idx == {"quick question", "deal terms"}


def test_unanswered_row_with_no_sent_reply_surfaces():
    rows = [
        {"action": "fire", "sender": "a@x.com", "subject": "Re: Partnership terms"},
        {"action": "fire", "sender": "b@y.com", "subject": "Invoice question"},
    ]
    sent = sent_stem_index(["Re: Partnership terms"])   # only the first was answered
    out = unanswered_archived(rows, sent)
    assert [r["sender"] for r in out] == ["b@y.com"]     # the answered one drops; the owed one stays


def test_non_fire_and_empty_stem_are_ignored():
    rows = [
        {"action": "archive", "sender": "c@z.com", "subject": "Newsletter"},   # not an obligation
        {"action": "fire", "sender": "d@z.com", "subject": ""},                 # unjoinable
        {"action": "fire", "sender": "e@z.com", "subject": "Real ask"},
    ]
    out = unanswered_archived(rows, set())
    assert [r["sender"] for r in out] == ["e@z.com"]


def test_everything_answered_yields_empty():
    rows = [{"action": "fire", "sender": "a@x.com", "subject": "Contract"}]
    assert unanswered_archived(rows, {"contract"}) == []


def test_pick_prefers_exact_then_suffix():
    # Gmail-style names → the "[Gmail]/All Mail" / "[Gmail]/Sent Mail" exact hits.
    gmail = ["INBOX", "[Gmail]/All Mail", "[Gmail]/Sent Mail", "[Gmail]/Drafts"]
    assert _pick(gmail, ARCHIVE_CANDIDATES) == "[Gmail]/All Mail"
    assert _pick(gmail, SENT_CANDIDATES) == "[Gmail]/Sent Mail"
    # iCloud/Outlook-style → plain "Archive" / "Sent".
    icloud = ["INBOX", "Archive", "Sent", "Trash"]
    assert _pick(icloud, ARCHIVE_CANDIDATES) == "Archive"
    assert _pick(icloud, SENT_CANDIDATES) == "Sent"
    # Nothing matching → None (caller skips the account).
    assert _pick(["INBOX", "Junk"], ARCHIVE_CANDIDATES) is None
