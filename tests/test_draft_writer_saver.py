"""Tests for draft_writer._select_saver — the transport selection that designs out the
macOS Automation grant.

Invariant: the KEYED, HEADLESS IMAP path is chosen iff the Gmail app-password is present
in the env; otherwise it FALLS BACK to the Apple-Mail path (today's behaviour). Neither
path sends — both only write a Draft. We distinguish the two by close_fn: the keyed path
carries a live connection to release (close_fn is not None); the Apple-Mail path does not.
It also returns handled_fn — the server-truth reconciliation check — which only the keyed
IMAP path can provide (it alone sees Sent/Drafts on the server); it is None otherwise.
"""

import draft_writer
from providers.imap import IMAPProvider


def test_select_saver_none_when_not_saving():
    assert draft_writer._select_saver(False) == (None, None, None)


def test_select_saver_prefers_keyed_imap_when_app_password_present(monkeypatch):
    monkeypatch.setenv("GMAIL_USER", "u@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-pw")  # allow-secret: test literal
    calls = []

    def fake_create_draft(self, to_addr, subject, body, account=None):
        calls.append((to_addr, subject, body, account, self.user))
        return True

    def fake_handled(self, to_addr, subject):
        return False

    # patch on the class so _select_saver's local import binds the patched methods;
    # construction stays offline (connect() is lazy, only reached inside the methods).
    monkeypatch.setattr(IMAPProvider, "create_draft", fake_create_draft)
    monkeypatch.setattr(IMAPProvider, "thread_already_handled", fake_handled)

    save_fn, close_fn, handled_fn = draft_writer._select_saver(True)
    assert close_fn is not None                        # keyed path → a connection to close
    assert handled_fn is not None                      # keyed path → server-truth reconcile
    assert handled_fn("boss@corp.com", "Re: Plan") is False
    assert save_fn("boss@corp.com", "Re: Plan", "ok\n", account="Gmail") is True
    assert calls == [("boss@corp.com", "Re: Plan", "ok\n", "Gmail", "u@gmail.com")]
    close_fn()                                         # cleanup must never raise


def test_select_saver_falls_back_without_key(monkeypatch):
    for var in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "IMAP_USER", "IMAP_PASS"):
        monkeypatch.delenv(var, raising=False)
    _save_fn, close_fn, handled_fn = draft_writer._select_saver(True)
    # Apple-Mail fallback (or enrich-only) carries no keyed connection to close,
    # and cannot reconcile against the server → handled_fn is None.
    assert close_fn is None
    assert handled_fn is None
