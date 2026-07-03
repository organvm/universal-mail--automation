"""sweep_starred_noise unstars only the stars the classifier marks as noise.

This tests the SWEEP MECHANISM in isolation: given a classification, it must
unstar exactly the action=='archive' rows and leave keep/fire (protected) rows
starred. The classifier itself — including the fail-closed protected-sender
allowlist that CI restores from PROTECTED_SENDERS_EXTRA before any --apply — is
covered by test_inbox_sweep_decide.py; here we monkeypatch classify() so the
sweep's own logic is what's under test, not the rules engine.
"""

import gmail_imap_sweep as sweep


class FakeProvider:
    def __init__(self, unstar_fail=()):
        self.unstarred = []
        self._fail = set(unstar_fail)

    def unstar(self, uid):
        self.unstarred.append(uid)
        return uid not in self._fail


def _rows(*specs):
    # specs: (uid, action)
    return [{"uid": u, "sender": f"{u}@x", "subject": "s", "action": a,
             "label": "", "tier": "", "protected": a != "archive",
             "is_starred": True} for (u, a) in specs]


def _patch(monkeypatch, rows):
    monkeypatch.setattr(sweep, "classify", lambda provider, mailbox, limit: rows)


def test_apply_unstars_noise_only(monkeypatch):
    _patch(monkeypatch, _rows(("1", "archive"), ("2", "archive"),
                              ("10", "keep"), ("11", "fire")))
    p = FakeProvider()
    out = sweep.sweep_starred_noise(p, limit=100, apply=True)
    assert out["available"] is True
    assert set(p.unstarred) == {"1", "2"}          # noise only
    assert "10" not in p.unstarred and "11" not in p.unstarred
    assert out["unstarred"] == 2 and out["unstar_errors"] == 0


def test_dry_run_touches_nothing_but_counts(monkeypatch):
    _patch(monkeypatch, _rows(("1", "archive"), ("2", "archive"), ("10", "keep")))
    p = FakeProvider()
    out = sweep.sweep_starred_noise(p, limit=100, apply=False)
    assert p.unstarred == []
    assert out["noise"] == 2 and out["unstarred"] == 0


def test_unstar_error_is_counted(monkeypatch):
    _patch(monkeypatch, _rows(("1", "archive"), ("2", "archive")))
    p = FakeProvider(unstar_fail={"2"})
    out = sweep.sweep_starred_noise(p, limit=100, apply=True)
    assert out["unstarred"] == 1 and out["unstar_errors"] == 1


def test_unavailable_mailbox_is_fail_soft(monkeypatch):
    def boom(provider, mailbox, limit):
        raise RuntimeError("Failed to select mailbox: [Gmail]/Starred")
    monkeypatch.setattr(sweep, "classify", boom)
    out = sweep.sweep_starred_noise(FakeProvider(), limit=100, apply=True)
    assert out["available"] is False and out["unstarred"] == 0
