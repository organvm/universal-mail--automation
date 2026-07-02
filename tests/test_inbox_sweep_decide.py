"""Predicate for the flagged-newsletter-storm fix (inbox_sweep.decide / is_bulk_sender).

Root cause: looks_human() fired on nearly every newsletter that fronts a "First Last"
display name, so the heartbeat flagged them as "fires" and the flag pile filled with
newsletters. These tests lock in: (a) bulk/newsletter mailboxes are detected, (b) genuine
obligations + real people still fire, (c) newsletters no longer fire, (d) protected
senders are never archived.

PRIVACY: this is a PUBLIC repo — every sender/subject below is SYNTHETIC, mirroring the
real failure shapes without any operator PII (mirrors core/rules.py's example policy).
"""
from inbox_sweep import decide, is_bulk_sender


# --- is_bulk_sender: role local-parts + bulk-ESP subdomains, exact-match only ---------

def test_bulk_role_localparts():
    assert is_bulk_sender("Widget Team <welcome@widget.example>")
    assert is_bulk_sender("Refer A Friend <referrals@example.dev>")
    assert is_bulk_sender("Example Offers <offers@example.com>")
    assert is_bulk_sender("Example Post <post@post.example>")
    assert is_bulk_sender("Example Academy <news@example.com>")


def test_bulk_esp_subdomains():
    assert is_bulk_sender("First Last <someone@mail.example.com>")
    assert is_bulk_sender("Shop Name <marketplace@email.example.com>")
    assert is_bulk_sender("Streamer <info@members.example.com>")


def test_real_people_are_not_bulk():
    # exact-match semantics: a real personal address is never swept in
    assert not is_bulk_sender("Jane Roe <jane@startup.example>")
    assert not is_bulk_sender("John Doe <john@company.example>")
    assert not is_bulk_sender("Sam Smith <sam.smith@founder.example>")


# --- decide: obligations fire, newsletters archive, protected never archived ----------

def test_genuine_obligations_still_fire():
    # ACTION_SIGNALS carry a real obligation regardless of sender shape
    assert decide("Payments <notifications@example.com>",
                  "[Action required] Provide information about your account", 2, False,
                  "Finance/Payments") == "fire"
    assert decide("Loan Servicer <noreply@servicer.example>",
                  "Final reminder - your loan is about to default", 1, False,
                  "Misc/Other") == "fire"
    assert decide("Vendor Billing <failed-payments@mail.vendor.example>",
                  "Your payment was unsuccessful", 3, False, "AI/Services") == "fire"


def test_real_person_non_promotional_fires():
    # a real person's clean address + non-promotional subject still surfaces
    assert decide("Jane Roe <jane@startup.example>",
                  "Engineering role — quick intro?", 4, False, "Misc/Other") == "fire"
    assert decide("Sam Smith <sam.smith@firm.example>",
                  "Consent form", 4, False, "Misc/Other") == "fire"


def test_newsletters_no_longer_fire():
    # human-looking display name + bulk address -> archive (not fire)
    assert decide("Widget Team <welcome@widget.example>",
                  "What shipped this month", 4, False, "Misc/Other") == "archive"
    assert decide("Person Name <ivan@mail.product.example>",
                  "Product 3.6: new features", 3, False, "AI/Services") == "archive"
    # Marketing/Entertainment label beats a human-looking display name
    assert decide("Newsletter Team <hello@list.example>", "Monthly insider digest", 4,
                  False, "Marketing") == "archive"
    assert decide("Shop <deals@email.example.com>", "Claim your coupon", 4, False,
                  "Marketing") == "archive"


def test_protected_newsletter_is_kept_never_archived():
    # a protected sender's promotional mail is KEPT in inbox, never moved out
    assert decide("Big Vendor <no_reply@email.bigvendor.example>",
                  "Your receipt from Big Vendor", 4, True, "Misc/Other") == "keep"
