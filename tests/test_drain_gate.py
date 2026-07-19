"""Drain-gate tests: an obligation answered out-of-band drains OUT of the ledger.

The walk (correspondence-walk.py) detects a reply in [Gmail]/Sent and records the row's _ob_key
to audit/answered_keys.json; obligations_build.build() then retires those rows so reply_owed
falls monotonically instead of re-materializing every beat. Fail-open: an absent/garbage set
drops nothing, so a genuine reply-owed row is never silenced.

Samples are synthetic and mirror a real reply-owed shape without embedding private mail.
"""

import json

from obligations_build import build, _ob_key, _load_answered_keys


def _authplane_receipt():
    # A real personal correspondent, no bulk headers ⇒ stays reply-owed until answered.
    return {
        "result": {"account": "acct", "total": 1},
        "rows": [
            {
                "id": "r1", "action": "fire",
                "sender": "Uruba Niazi <uruba.niazi@authplane.ai>",
                "subject": "quick question on FastMCP + auth", "tier": 4,
            }
        ],
    }


def test_answered_row_drains_out_of_ledger(tmp_path):
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(_authplane_receipt()), encoding="utf-8")

    # Before: the row is reply-owed and present.
    ledger = build(str(tmp_path))
    reply_owed = [o for o in ledger["obligations"] if o["requires_reply"]]
    assert [o["domain"] for o in reply_owed] == ["authplane.ai"]
    key = _ob_key(reply_owed[0])

    # Record it as answered out-of-band → it drains out; reply_owed falls by exactly one.
    (tmp_path / "answered_keys.json").write_text(json.dumps([key]), encoding="utf-8")
    drained = build(str(tmp_path))
    assert all(_ob_key(o) != key for o in drained["obligations"])
    assert drained["totals"]["obligations"] == ledger["totals"]["obligations"] - 1


def test_unrelated_answered_key_drops_nothing(tmp_path):
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(_authplane_receipt()), encoding="utf-8")
    baseline = build(str(tmp_path))["totals"]["obligations"]

    # A key that matches no obligation must not silence a genuine reply-owed row.
    (tmp_path / "answered_keys.json").write_text(json.dumps(["no|such|000"]), encoding="utf-8")
    unchanged = build(str(tmp_path))
    assert unchanged["totals"]["obligations"] == baseline


def test_missing_or_torn_answered_file_fails_open(tmp_path):
    # Absent file ⇒ empty set (build already ran above with no file), and a torn file must also
    # yield the empty set rather than raise — exact status quo.
    (tmp_path / "answered_keys.json").write_text("{ not json", encoding="utf-8")
    assert _load_answered_keys(str(tmp_path)) == set()
