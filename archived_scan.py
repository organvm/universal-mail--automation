#!/usr/bin/env python3
"""archived_scan.py — surface archived-but-UNANSWERED threads the INBOX-only sweep can't see.

THE BLIND SPOT IT CLOSES: inbox_sweep.py reads INBOX only, so a thread that owed a reply but was
archived (by a triage pass, or by hand) drops out of the obligations ledger SILENTLY — and then
`reply_owed=0` means "nothing owed IN THE INBOX", not "nothing owed". This scanner is the answer to
"have we responded to everything?" that is independent of what happens to still be in the inbox.

It classifies the Archive / All-Mail mailbox with the SAME decide() logic the inbox sweep uses, then
keeps only the genuine obligations (action == "fire") whose subject stem has NO matching reply in the
Sent mailbox — i.e. an archived INBOUND that was never answered. The join is the subject stem (the
same key _ob_key uses across the estate) — a conservative diagnostic, not a send trigger.

READ-ONLY + count-first: unlike inbox_sweep it FLAGS nothing and MOVES nothing. It reports a count and
writes a receipt so a human (or obligations_build, later) decides. Fail-open: any provider error
yields an empty result, never a crash. No IMAP — pure Mail.app AppleScript, so it never trips the
Gmail IMAP rate-limit.

Usage:
  python3 archived_scan.py --account "a.j.padavano@icloud"            # dry report + receipt
  python3 archived_scan.py --account "…" --archive-mailbox Archive --sent-mailbox Sent --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

# The classifier is the inbox sweep's — reuse it verbatim so an archived thread is judged a genuine
# obligation by EXACTLY the same rule an inbox one is (no second, drifting classifier).
from inbox_sweep import classify_inbox

# Archive/All-Mail candidates, most-preferred first; Sent candidates likewise. Provider-specific
# names differ (Gmail "[Gmail]/All Mail" vs iCloud/Outlook "Archive"); we pick the first that exists.
ARCHIVE_CANDIDATES = ("[Gmail]/All Mail", "All Mail", "Archive", "Archived")
SENT_CANDIDATES = ("[Gmail]/Sent Mail", "Sent Mail", "Sent Messages", "Sent")


def _norm_subject(subject: str) -> str:
    """Bare subject stem: strip leading Re:/Fwd:/Fw: (repeatable, case-insensitive), lower, collapse
    whitespace. The SAME normalization the walk/_ob_key use, so an archived inbound and its Sent reply
    join on identical stems."""
    s = str(subject or "")
    while True:
        m = re.match(r"^\s*(re|fwd|fw)\s*:\s*", s, re.IGNORECASE)
        if not m:
            break
        s = s[m.end():]
    return re.sub(r"\s+", " ", s).strip().lower()


def sent_stem_index(sent_subjects) -> set[str]:
    """The set of normalized subject stems present in Sent — the 'already answered' index. PURE."""
    return {stem for stem in (_norm_subject(s) for s in sent_subjects) if stem}


def unanswered_archived(fire_rows, sent_stems: set[str]) -> list[dict]:
    """Given classified archive rows (from classify_inbox) and the Sent stem index, return the genuine
    obligations (action == 'fire') whose subject stem is NOT in Sent — archived + never answered. PURE
    and offline-testable: no provider, no I/O. A row with an empty stem is skipped (can't be joined)."""
    out = []
    for r in fire_rows:
        if r.get("action") != "fire":
            continue
        stem = _norm_subject(r.get("subject", ""))
        if stem and stem not in sent_stems:
            out.append(r)
    return out


def _pick(names, candidates) -> str | None:
    """First candidate present in `names` (case-insensitive exact, then suffix match for '/'-paths). PURE."""
    lower = {n.lower(): n for n in names}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    for c in candidates:  # suffix: an account may expose "All Mail" without the "[Gmail]/" prefix
        tail = c.split("/")[-1].lower()
        for n in names:
            if n.lower() == tail or n.lower().endswith("/" + tail):
                return n
    return None


def scan(provider, archive_name: str, sent_name: str, limit: int) -> dict:
    """Live scan: classify the archive mailbox, index Sent, return the unanswered obligations.
    Fail-open — a provider error on either mailbox degrades to an empty result, never raises."""
    try:
        archive_rows = classify_inbox(provider, archive_name, limit)
    except Exception as exc:  # noqa: BLE001 — fail-open: no archive read this run
        return {"error": f"archive classify failed ({type(exc).__name__})", "unanswered": []}
    try:
        sent_res = provider.list_messages(query="", limit=limit, mailbox=sent_name)
        sent_stems = sent_stem_index(m.subject or "" for m in sent_res.messages)
    except Exception:  # noqa: BLE001 — no Sent index ⇒ conservative: everything looks unanswered,
        sent_stems = set()  # so we still surface (never silently drop), but flag the degraded index.
    unanswered = unanswered_archived(archive_rows, sent_stems)
    return {
        "archive_scanned": len(archive_rows),
        "sent_indexed": len(sent_stems),
        "unanswered": unanswered,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan Archive/All-Mail for inbound threads never answered in Sent.")
    ap.add_argument("--account", required=True)
    ap.add_argument("--archive-mailbox", default=None, help="override; else auto-pick All Mail/Archive")
    ap.add_argument("--sent-mailbox", default=None, help="override; else auto-pick Sent")
    ap.add_argument("--limit", type=int, default=500, help="max messages per mailbox to scan")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--receipt", default=None, help="path to write the JSON receipt")
    args = ap.parse_args(argv)

    try:
        from providers.mailapp import MailAppProvider
        provider = MailAppProvider(account=args.account)
        names = provider.get_mailboxes()
    except Exception as exc:  # noqa: BLE001 — no Mail.app / no account ⇒ fail-open, report nothing
        print(f"archived_scan: Mail.app unavailable ({type(exc).__name__}) — skipping", file=sys.stderr)
        return 0

    archive_name = args.archive_mailbox or _pick(names, ARCHIVE_CANDIDATES)
    sent_name = args.sent_mailbox or _pick(names, SENT_CANDIDATES)
    if not archive_name or not sent_name:
        print(f"archived_scan: {args.account} — no archive/sent mailbox found "
              f"(archive={archive_name!r}, sent={sent_name!r}); skipping", file=sys.stderr)
        return 0

    result = scan(provider, archive_name, sent_name, args.limit)
    unanswered = result.get("unanswered", [])

    receipt = {
        "schema": "uma.archived_scan.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": args.account,
        "archive_mailbox": archive_name,
        "sent_mailbox": sent_name,
        "archive_scanned": result.get("archive_scanned", 0),
        "sent_indexed": result.get("sent_indexed", 0),
        "unanswered_count": len(unanswered),
        "unanswered": [
            {"sender": r.get("sender", ""), "subject": r.get("subject", ""),
             "tier": r.get("tier"), "label": r.get("label")}
            for r in unanswered
        ],
    }
    path = args.receipt or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "audit",
        f"archived_scan-{re.sub(r'[^A-Za-z0-9]+', '_', args.account)}.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(receipt, f, indent=2)
    except OSError as exc:
        print(f"archived_scan: receipt not written ({exc})", file=sys.stderr)

    if args.json:
        print(json.dumps({k: receipt[k] for k in
                          ("account", "archive_scanned", "sent_indexed", "unanswered_count")}))
    else:
        print(f"archived_scan: {args.account} — scanned {receipt['archive_scanned']} archived, "
              f"{receipt['sent_indexed']} sent-stems indexed → "
              f"{receipt['unanswered_count']} archived-but-UNANSWERED (receipt → {path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
