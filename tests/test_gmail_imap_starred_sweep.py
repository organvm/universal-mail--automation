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
        self.labeled = []          # (uid, label)
        self._fail = set(unstar_fail)

    def unstar(self, uid):
        self.unstarred.append(uid)
        return uid not in self._fail

    def apply_label(self, uid, label):
        self.labeled.append((uid, label))
        return True


def _rows(*specs):
    # specs: (uid, action) — distinct sender+subject per uid so the organizer's
    # duplicate-flag collapse doesn't fire on unrelated synthetic rows.
    return [{"uid": u, "sender": f"s{u}@d{u}.com", "subject": f"subject {u}", "action": a,
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


# -- _star_disposition: aggressive noise-clearing with a hard critical veto ----
def _row(sender, subject, action="keep"):
    return {"sender": sender, "subject": subject, "action": action}


def test_critical_keep_always_wins():
    # Every one of these must stay starred even if a noise pattern also matches.
    keep = [
        _row("nelnetnoreply@nelnet.studentaid.gov", "your loan is about to default"),
        _row("notifications@stripe.com", "[Action required] Provide information about Et4l"),
        _row("mlongo@longofirm.com", "Interrogatory Responses | Review Answers"),
        _row("zafer@algora.io", "Air Space Intelligence interview"),
        _row("noreply@email.legalzoom.com", "Keep your business compliant"),
        _row("legal@taxrise.com", "Refund Misdirected – Demand"),
        _row("padavano.anthony@gmail.com", "Re: Discovery Documents"),
        _row("no@ssa.gov", "Your Replacement Social Security Card"),
        # critical veto beats a matching noise subject:
        _row("nelnetnoreply@nelnet.studentaid.gov", "your statement is ready"),
    ]
    for r in keep:
        assert sweep._star_disposition(r) == "keep", r["subject"]


def test_transactional_and_newsletter_noise_unstars():
    noise = [
        _row("alerts@account.chime.com", "Your deposit is now available"),
        _row("alerts@account.chime.com", "PIN change confirmation"),
        _row("noreply@context7.com", "New device signed in to your Context7 account"),
        _row("notifications@stripe.com", "Your Stripe password has been updated"),
        _row("copilot@app.collabwriting.com", "Copilot's Weekly Report"),
        _row("no-reply@accounts.nintendo.com", "Nintendo Receipt: Funds Added"),
        _row("noreply@notify.cloudflare.com", "[ACTION REQUIRED] Daily request limit exceeded"),
        _row("x@y.com", "spam", action="archive"),   # classifier-noise still unstars
    ]
    for r in noise:
        assert sweep._star_disposition(r) == "unstar", r["subject"]


# -- matter classification + duplicate-flag collapse --------------------------
def test_matter_classification():
    cases = [
        ("mlongo@longofirm.com", "Deposition summary", "Open Matters/Litigation"),
        ("legal@taxrise.com", "Refund misdirected", "Open Matters/Tax"),
        ("nelnetnoreply@nelnet.studentaid.gov", "prevent wage garnishment", "Open Matters/Student Loan"),
        ("notifications@stripe.com", "Provide information about Et4l", "Open Matters/Identity & KYC"),
        ("zafer@algora.io", "Air Space Intelligence interview", "Open Matters/Job Search"),
        ("noreply@reservations.dmv.ny.gov", "Reservation Confirmation", "Open Matters/Government"),
        ("CloudPlatform-noreply@google.com", "account overdue - Action Required", "Open Matters/Billing"),
        ("alerts@account.chime.com", "Your Chime Card statement is ready", "Open Matters/Banking"),
        ("someone@gmail.com", "hey are we still on for lunch", "Open Matters/Personal"),
    ]
    for sender, subj, expected in cases:
        assert sweep._matter(sender, subj) == expected, (sender, subj)


def test_norm_subject_collapses_ids_and_dates():
    assert sweep._norm_subject("Google Cloud account (ID 016B52-CC5865) overdue") == \
           sweep._norm_subject("Google Cloud account (ID 016B52-CC5865-3BDA82) overdue")
    assert sweep._norm_subject("Re: TaxRise Ticket Closed – 00059926") == \
           sweep._norm_subject("TaxRise Ticket Closed – 00063595")


def test_organize_labels_all_and_collapses_duplicate_flags():
    # 3 identical-subject GCP billing flags (diff uids) + 1 distinct loan flag.
    keepers = [
        {"uid": "100", "sender": "CloudPlatform-noreply@google.com", "subject": "account 016B52 overdue"},
        {"uid": "105", "sender": "CloudPlatform-noreply@google.com", "subject": "account 016B52 overdue"},
        {"uid": "110", "sender": "CloudPlatform-noreply@google.com", "subject": "account 016B52 overdue"},
        {"uid": "200", "sender": "nelnetnoreply@nelnet.studentaid.gov", "subject": "prevent garnishment"},
    ]
    p = FakeProvider()
    out = sweep.organize_flagged(p, keepers, apply=True)
    # every keeper got a matter label
    assert out["labeled"] == 4
    assert {lbl for _, lbl in p.labeled} == {"Open Matters/Billing", "Open Matters/Student Loan"}
    # the 3 GCP dupes collapse to 1 flag: the 2 older uids un-starred, newest (110) kept
    assert out["deduped"] == 2
    assert set(p.unstarred) == {"100", "105"}
    assert "110" not in p.unstarred and "200" not in p.unstarred


def test_organize_dry_run_touches_nothing():
    keepers = [{"uid": "1", "sender": "x@taxrise.com", "subject": "case update"}]
    p = FakeProvider()
    out = sweep.organize_flagged(p, keepers, apply=False)
    assert p.labeled == [] and p.unstarred == []
    assert out["matters"] == {"Open Matters/Tax": 1}


def test_end_to_end_unstars_noise_keeps_critical(monkeypatch):
    rows = [
        _row("nelnetnoreply@nelnet.studentaid.gov", "prevent wage garnishment"),  # keep
        _row("alerts@account.chime.com", "Your deposit is now available"),        # unstar
        _row("zafer@algora.io", "interview"),                                     # keep
        _row("no-reply@accounts.nintendo.com", "Nintendo Receipt: Funds Added"),  # unstar
    ]
    for r in rows:
        r["uid"] = r["subject"][:6]
        r["is_starred"] = True
    monkeypatch.setattr(sweep, "classify", lambda p, m, l: rows)
    p = FakeProvider()
    out = sweep.sweep_starred_noise(p, limit=100, apply=True)
    assert out["unstarred"] == 2
    # only the two noise rows (uid = subject[:6]) were unstarred
    assert set(p.unstarred) == {"Your d", "Ninten"}
    assert "preven" not in p.unstarred   # nelnet garnishment kept
    assert "interv" not in p.unstarred   # algora interview kept
