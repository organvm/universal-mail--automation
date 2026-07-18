"""Pure-core tests for archived_scan.py — the archived-but-unanswered detector.

No Mail.app, no provider, no I/O: the join logic (subject-stem match against the Sent index) is a
pure function, so an archived inbound with no reply in Sent surfaces, and one whose stem IS in Sent
does not. This is the "have we responded to everything?" check made independent of the inbox.
"""

from archived_scan import (
    _addressable,
    _norm_subject,
    _pick,
    reply_owed,
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


def test_reply_owed_suppresses_bulk_headered_archived_rows():
    """The live-run false-alarm fix: classify_inbox marks a newsletter 'fire' PRE-suppression, so
    an archived Apple/marketing storm used to count as 'unanswered'. reply_owed() runs the SAME
    derive() cascade obligations_build uses, so a List-Unsubscribe row is dropped (cls=bulk) while a
    genuine personal ask survives."""
    bulk = {"action": "fire", "sender": "Apple <no-reply@email.apple.com>",
            "subject": "This week on Apple", "tier": 4, "label": "Misc/Other",
            "headers": {"list-unsubscribe": "<https://apple.com/unsub>"}}
    personal = {"action": "fire", "sender": "Uruba Niazi <uruba.niazi@authplane.ai>",
                "subject": "quick question on FastMCP + auth", "tier": 2, "label": "Business"}

    # The predicate itself: bulk suppressed, personal kept.
    assert reply_owed(bulk) is False
    assert reply_owed(personal) is True

    rows = [bulk, personal]
    # WITH the predicate (what scan() passes): only the personal ask is archived-but-unanswered.
    owed = unanswered_archived(rows, sent_stems=set(), requires_reply=reply_owed)
    assert [r["sender"] for r in owed] == ["Uruba Niazi <uruba.niazi@authplane.ai>"]
    # WITHOUT it (legacy): BOTH survive — proving the suppression is exactly what changed.
    assert len(unanswered_archived(rows, sent_stems=set())) == 2


def test_outlook_sent_items_is_a_sent_candidate():
    """Outlook's Sent folder is 'Sent Items' — without it in SENT_CANDIDATES the account skips
    (the estate-scan finding: Outlook returned sent=None)."""
    outlook = ["Inbox", "Archive", "Sent Items", "Deleted Items", "Junk Email"]
    assert _pick(outlook, SENT_CANDIDATES) == "Sent Items"
    assert _pick(outlook, ARCHIVE_CANDIDATES) == "Archive"


def test_addressable_maps_gmail_specials_to_bracket_prefix():
    """Gmail specials list by bare name but only ADDRESS via '[Gmail]/'. _addressable remaps them
    for Gmail accounts and leaves everyone else (and already-prefixed names) untouched."""
    g = "padavano.anthony@gmail"
    assert _addressable(g, "All Mail") == "[Gmail]/All Mail"
    assert _addressable(g, "Sent Mail") == "[Gmail]/Sent Mail"
    assert _addressable(g, "[Gmail]/All Mail") == "[Gmail]/All Mail"   # already addressable
    # A Gmail user-label (not a special) is a real addressable mailbox — left as-is.
    assert _addressable(g, "Banking") == "Banking"
    # Non-Gmail accounts: never prefixed, even for a same-named folder.
    assert _addressable("a.j.padavano@icloud", "Archive") == "Archive"
    assert _addressable("ajpadavano@outlook", "Sent Items") == "Sent Items"
    assert _addressable(g, None) is None
