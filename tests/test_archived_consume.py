"""Archived-consume tests: obligations_build folds archived_scan receipts into the ledger.

A thread that owed a reply but was archived never reaches the INBOX sweep, so obligations_build
also reads audit/archived_scan-*.json. Three guarantees are exercised here:
  1. a personal archived-but-unanswered row surfaces as an obligation;
  2. noise-hardening — an archived bulk/noreply row is re-suppressed by the SAME derive() cascade;
  3. dedup — a thread present in BOTH the inbox fire and the archive scan counts exactly once.

Samples are synthetic and mirror real shapes without embedding private mail.
"""

import json

from obligations_build import build, load_archived_receipts


def _inbox_receipt(rows):
    return {"result": {"account": "acct", "total": len(rows)}, "rows": rows}


def _archived_receipt(account, unanswered, generated_at="2026-07-18T00:00:00+00:00"):
    return {
        "schema": "uma.archived_scan.v1",
        "generated_at": generated_at,
        "account": account,
        "unanswered": unanswered,
    }


def test_personal_archived_row_folds_into_ledger(tmp_path):
    # No inbox fires — the obligation exists ONLY in the archive (the blind spot this closes).
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(_inbox_receipt([])), encoding="utf-8")
    (tmp_path / "archived_scan-acct.json").write_text(json.dumps(_archived_receipt(
        "acct", [{"sender": "Uruba Niazi <uruba.niazi@authplane.ai>",
                  "subject": "quick question on FastMCP + auth", "tier": 4, "label": "Business"}]
    )), encoding="utf-8")

    ledger = build(str(tmp_path))
    owed = [o for o in ledger["obligations"] if o["requires_reply"]]
    assert [o["domain"] for o in owed] == ["authplane.ai"]
    assert owed[0].get("archived_occurrences") == 1
    assert ledger["totals"]["archived_unanswered"] == 1


def test_archived_noreply_row_is_noise_suppressed(tmp_path):
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(_inbox_receipt([])), encoding="utf-8")
    # A noreply/newsletter sender: derive() suppresses it (requires_reply False) even with no headers
    # in the receipt — so it must NOT be folded, even if it slipped into the receipt.
    (tmp_path / "archived_scan-acct.json").write_text(json.dumps(_archived_receipt(
        "acct", [{"sender": "Apple <no-reply@email.apple.com>",
                  "subject": "This week on Apple", "tier": 4, "label": "Misc/Other"}]
    )), encoding="utf-8")

    ledger = build(str(tmp_path))
    assert ledger["totals"]["archived_unanswered"] == 0
    assert [o for o in ledger["obligations"] if o["requires_reply"]] == []


def test_archived_row_dedups_against_inbox_fire(tmp_path):
    # The SAME thread is both a live inbox fire AND surfaced by the archive scan (Re: prefix differs).
    inbox = _inbox_receipt([{
        "id": "r1", "action": "fire", "sender": "Uruba Niazi <uruba.niazi@authplane.ai>",
        "subject": "quick question on FastMCP + auth", "tier": 4,
    }])
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(inbox), encoding="utf-8")
    (tmp_path / "archived_scan-acct.json").write_text(json.dumps(_archived_receipt(
        "acct", [{"sender": "Uruba Niazi <uruba.niazi@authplane.ai>",
                  "subject": "Re: quick question on FastMCP + auth", "tier": 4, "label": "Business"}]
    )), encoding="utf-8")

    ledger = build(str(tmp_path))
    owed = [o for o in ledger["obligations"] if o["domain"] == "authplane.ai"]
    assert len(owed) == 1
    assert owed[0]["occurrences"] == 1                     # counted once, not twice
    assert owed[0].get("archived_occurrences") in (None, 0)  # the archive row was deduped away
    assert ledger["totals"]["archived_unanswered"] == 0


def test_load_archived_receipts_keeps_newest_per_account(tmp_path):
    # Two files map to the same logical account; only the newest generated_at is folded.
    (tmp_path / "archived_scan-acct_old.json").write_text(json.dumps(_archived_receipt(
        "acct", [{"sender": "old@x.com", "subject": "stale ask", "tier": 4}],
        generated_at="2026-07-01T00:00:00+00:00")), encoding="utf-8")
    (tmp_path / "archived_scan-acct_new.json").write_text(json.dumps(_archived_receipt(
        "acct", [{"sender": "new@x.com", "subject": "fresh ask", "tier": 4}],
        generated_at="2026-07-18T00:00:00+00:00")), encoding="utf-8")

    loaded = dict((a, rows) for a, rows in load_archived_receipts(str(tmp_path)))
    assert list(loaded.keys()) == ["acct"]
    assert [r["subject"] for r in loaded["acct"]] == ["fresh ask"]
