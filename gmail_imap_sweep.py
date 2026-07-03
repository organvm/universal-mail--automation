#!/usr/bin/env python3
"""gmail_imap_sweep.py — archive Gmail noise by REMOVING the INBOX label over raw IMAP.

WHY this exists: Apple Mail cannot archive a Gmail *label* store. A "move to All Mail"
is a no-op on the INBOX label (every message is already in All Mail), so Gmail re-asserts
INBOX on the next sync — VERIFIED 2026-06-22: after Apple Mail reported archived=18, the
threads still carried INBOX on Google's servers. Raw IMAP CAN archive: the Gmail
X-GM-LABELS extension lets us drop the ``\\Inbox`` label, a TRUE archive that sticks
(providers/imap.py::archive -> remove_label(uid, "\\Inbox"), success only on a server OK).

This reuses the EXACT SAME classifier as inbox_sweep.py (core.rules + decide), so the
fail-closed protected-sender gate and the operator's local never-archive allowlist
(config/protected_senders.local.txt, gitignored) hold identically — the dry-run
classification already verified on the Apple-Mail path carries over unchanged.

Reversible: archive drops ONLY the INBOX label; every thread stays in All Mail, and the
JSON receipt records uid/sender/subject of everything touched = the exact undo manifest.
Fires are flagged (a status bit). NEVER deletes, NEVER sends. Dry-run by default; --apply
executes.

Auth (keyless after one setup): IMAP_USER + a Gmail APP PASSWORD via IMAP_PASS or a
1Password item (OP_ACCOUNT/OP_ITEM/OP_FIELD). An app password is required — OAuth/Apple-
Mail tokens cannot do a raw IMAP LOGIN.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from providers.imap import IMAPProvider          # noqa: E402
from core.rules import categorize_with_tier, is_protected_sender  # noqa: E402
from inbox_sweep import decide                    # noqa: E402  (same verified classifier)


def classify(provider, mailbox, limit):
    """List the mailbox and classify each message with the shared rules engine."""
    res = provider.list_messages(query="ALL", limit=limit, mailbox=mailbox)
    rows = []
    for m in res.messages:
        d = provider.get_message_details(m.id)
        if d is None:
            continue
        sender, subject = d.sender or "", d.subject or ""
        protected = is_protected_sender(sender)
        cat = categorize_with_tier(sender, subject)
        rows.append({
            "uid": m.id, "sender": sender, "subject": subject,
            "label": cat.label, "tier": cat.tier, "protected": protected,
            "is_starred": d.is_starred,
            "action": decide(sender, subject, cat.tier, protected),
        })
    return rows


STARRED_MAILBOX = "[Gmail]/Starred"


# Transactional / resolved / newsletter subjects that are noise even when the
# conservative inbox classifier keeps their (financial/gov) SENDER. These get
# un-starred so the flag pile reflects real obligations, not receipts. Scoped to
# the STAR sweep only — the inbox archive stays conservative.
STAR_NOISE_SUBJECT = re.compile(r"""(?ix)
    deposit\ is\ now | funds\ added | \bdeposit\b.{0,20}available
  | pin\ (change|updated|confirmation) | atm\ pin
  | statement\ is\ (here|ready|available|now) | card\ statement
  | order\ complete | confirmation\ of | purchase\ from | \breceipt\b
  | new\ device\ signed\ in | new\ sign.?in | signed\ in\ to\ your | unrecognized\ device
  | password\ (was\ |has\ been\ )?(updated|reset|changed) | reset\ your\ .{0,20}password
  | auto.?renewal | renews\ in\ \d | subscription\ renews | membership\ (has\ turned|renewal|is\ set)
  | security\ update\ for | update\ (any\ )?.{0,20}(app|macos)
  | verification\ code | secure\ code | one.?time\ (passcode|code)
  | daily\ .{0,20}limit | limit\ exceeded | request\ limit
  | weekly\ (report|recap) | \bnewsletter\b | insider | launch\ week | \bdigest\b | \brecap\b
  | new\ phone\ number\ added | confirm\ your\ email | email\ not\ found
  | credit\ score | credit\ insight
  | covered\ by\ spotme | balance\ is\ low | bank\ holiday | isn.?t\ available
  | privacy\ (notice|policy) | we.?ve\ updated\ our
  | replacement\ card\ is\ on\ the\ way
  | share\ your\ feedback | quick\ question\ about
""")

# HARD keep-list — a star matching ANY of these is NEVER unstarred, no matter
# what, so aggressive noise-clearing can't touch a real obligation.
STAR_CRITICAL_KEEP = re.compile(r"""(?ix)
    nelnet | studentaid | \bloan\b | default | garnish | \bwage
  | provide\ information | \bkyc\b | et4l
  | legalzoom
  | longo | attorney | litigation | discovery | deposition | interrogator
  | mediation | docusign | subpoena | \bcourt\b | verdict | \bjury\b
  | plaintiff | defendant | lawsuit | retaliation | \bmdc\b | zapata
  | taxrise
  | interview | recruiter | staffing
  | algora | stage4solutions | ceiamerica | insight | perficient
  | zafer | saikumar | kharwadkar | \bnaga\b
  | senator | lanza | constituent | pucciarelli
  | social\ security\ card | replacement\ social\ security
  | padavano\.anthony@gmail\.com
""")


def _star_disposition(row):
    """'keep' | 'unstar'. A CRITICAL match keeps unconditionally; otherwise the
    conservative classifier's 'archive' verdict OR a transactional/newsletter
    subject means noise → unstar."""
    text = f"{row.get('sender', '')}\n{row.get('subject', '')}"
    if STAR_CRITICAL_KEEP.search(text):
        return "keep"
    if row.get("action") == "archive" or STAR_NOISE_SUBJECT.search(row.get("subject", "")):
        return "unstar"
    return "keep"


# Every star we KEEP is sorted into its actual matter, so the flag list becomes a
# navigable index of open loops instead of a heap. Ordered: specific → general,
# first match wins.
MATTER_MAP = [
    ("Open Matters/Litigation", r"longo|\bmdc\b|zapata|docusign|mediation|deposition|interrogator|discovery|subpoena|verdict|\bjury\b|retaliation|plaintiff|defendant|lawsuit|jbfcs|consent\ form"),
    ("Open Matters/Tax", r"taxrise|\birs\b|tax\ (refund|resolution|debt|return|misdirected)"),
    ("Open Matters/Student Loan", r"nelnet|studentaid|\bfsa\ id|student\ loan|garnish|loan.*default|dept.*education|department\ of\ education"),
    ("Open Matters/Identity & KYC", r"\bkyc\b|provide\ information|et4l|legalzoom|id\.me|login\.gov|verify\ your\ identity|registered\ agent"),
    ("Open Matters/Job Search", r"algora|stage4|ceiamerica|insight\ global|perficient|recruiter|interview|zafer|saikumar|kharwadkar|\bnaga\b|hiring|opportunity|staffing|\brole\b"),
    ("Open Matters/Government", r"\bssa\b|social\ security|\bdmv\b|flhsmv|passport|senator|lanza|constituent|pucciarelli|\.gov\b"),
    ("Open Matters/Billing", r"google\ cloud|\bgcp\b|cloudplatform|github.*bill|had\ a\ problem\ billing|billing|godaddy|hostinger|adobe|backblaze|cloudflare|dropbox|parallels|atlassian|overdue|past\ due|payment\ (declined|failed|unsuccessful|overdue)|invoice|subscription.*(suspend|paus)|anthropic|openai"),
    ("Open Matters/Banking", r"chime|alliant|capital\ one|\bnav\b|santander|cash\ app|amazon.*payment|\bdeposit\b|statement|sezzle|onepay"),
]
DEFAULT_MATTER = "Open Matters/Personal"
_MATTER_RE = [(lbl, re.compile(pat, re.I)) for lbl, pat in MATTER_MAP]


def _matter(sender, subject):
    text = f"{sender} {subject}"
    for lbl, rx in _MATTER_RE:
        if rx.search(text):
            return lbl
    return DEFAULT_MATTER


def _norm_subject(s):
    """Normalise a subject for de-dup: drop Re:/Fwd:, digits (ticket #s, IDs,
    dates), punctuation/emoji, so near-identical repeats collapse to one key."""
    s = (s or "").lower()
    s = re.sub(r"^\s*(re|fwd|fw):\s*", "", s)
    s = re.sub(r"\b\w*\d\w*\b", " ", s)      # drop any token with a digit (IDs, ticket#s, dates)
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:50]


def organize_flagged(provider, keepers, apply):
    """Turn the KEPT flags into an organized index: label each by matter, then
    collapse duplicate flags — within (matter, sender-domain, normalised-subject)
    keep only the NEWEST star (highest UID), un-star the redundant older copies
    (they keep the matter label, so nothing is lost — just de-cluttered). Labels
    are additive/reversible; un-star is a \\Flagged bit."""
    for r in keepers:
        r["matter"] = _matter(r.get("sender", ""), r.get("subject", ""))
    from collections import Counter
    matters = Counter(r["matter"] for r in keepers)
    labeled = label_err = deduped = dedup_err = 0
    if apply:
        for r in keepers:
            if provider.apply_label(r["uid"], r["matter"]):
                labeled += 1
            else:
                label_err += 1
        groups = {}
        for r in keepers:
            dom = (r.get("sender", "").split("@")[-1] or "").strip("> ").lower()
            groups.setdefault((r["matter"], dom, _norm_subject(r.get("subject", ""))), []).append(r)
        for grp in groups.values():
            if len(grp) < 2:
                continue
            ordered = sorted(grp, key=lambda x: int(x["uid"]) if str(x["uid"]).isdigit() else 0)
            for r in ordered[:-1]:            # keep newest starred, un-star the rest
                if provider.unstar(r["uid"]):
                    deduped += 1
                    r["deduped"] = True
                else:
                    dedup_err += 1
    print("  [organize] matters: " + ", ".join(f"{m.split('/')[-1]}={n}"
                                                for m, n in matters.most_common()))
    if apply:
        print(f"  [organize] labeled={labeled} (err={label_err})  "
              f"duplicate-flags collapsed={deduped} (err={dedup_err})")
    return {"matters": dict(matters), "labeled": labeled, "label_errors": label_err,
            "deduped": deduped, "dedup_errors": dedup_err,
            "distinct_after_dedup": len(keepers) - deduped}


def sweep_starred_noise(provider, limit, apply):
    """Drain the RESIDUAL flag pile: stars on threads that may already have left
    the inbox (so the inbox pass never sees them). Classify ``[Gmail]/Starred``
    and unstar noise — the conservative classifier's 'archive' verdict PLUS
    transactional/resolved/newsletter subjects (receipts, PIN/deposit/statement
    confirmations, resolved security alerts) that the inbox classifier keeps by
    sender. A hard CRITICAL keep-list (loan, KYC, litigation, recruiters, SSA
    card, own sent mail) is NEVER unstarred. Reversible (a ``\\Flagged`` bit);
    never archives or deletes. Fail-soft if [Gmail]/Starred is unavailable."""
    try:
        rows = classify(provider, STARRED_MAILBOX, limit)
    except Exception as e:
        print(f"  [starred] skipped ({STARRED_MAILBOX} unavailable): {e}")
        return {"available": False, "total": 0, "noise": 0,
                "unstarred": 0, "unstar_errors": 0, "rows": []}
    noise = [r for r in rows if _star_disposition(r) == "unstar"]
    keepers = [r for r in rows if _star_disposition(r) == "keep"]
    unstarred = err = 0
    if apply:
        for r in noise:
            if provider.unstar(r["uid"]):
                unstarred += 1
                r["unstarred"] = True
            else:
                err += 1
                r["unstarred"] = False
    tail = (f"  UNSTARRED={unstarred} (errors={err})" if apply else "  (dry run)")
    print(f"  [starred] {len(rows)} starred — noise(unstar)={len(noise)}  "
          f"keep={len(keepers)}{tail}")
    # Organize the survivors into matters + collapse duplicate flags.
    organized = organize_flagged(provider, keepers, apply)
    return {"available": True, "total": len(rows), "noise": len(noise),
            "unstarred": unstarred, "unstar_errors": err,
            "organized": organized, "rows": rows}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Gmail IMAP sweep — archive noise by dropping the INBOX label (dry run by default).")
    ap.add_argument("--user", default=os.getenv("IMAP_USER"),
                    help="mailbox address (or set IMAP_USER); no default — names are not hardcoded")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--mailbox", default="INBOX")
    ap.add_argument("--apply", action="store_true",
                    help="actually flag/archive (default: dry run, no changes)")
    ap.add_argument("--receipt", default=None, help="path to write the JSON receipt / undo manifest")
    ap.add_argument("--no-starred", dest="sweep_starred", action="store_false", default=True,
                    help="skip the residual-star sweep of [Gmail]/Starred (default: also sweep it)")
    args = ap.parse_args(argv)
    if not args.user:
        ap.error("no mailbox configured — set IMAP_USER or pass --user <address>")

    provider = IMAPProvider(user=args.user, use_gmail_extensions=True)
    provider.connect()
    try:
        rows = classify(provider, args.mailbox, args.limit)
        acts = Counter(r["action"] for r in rows)
        print(f"=== {args.user}  {args.mailbox}  — {len(rows)} messages ===")
        print(f"  FIRE(flag)={acts['fire']}   KEEP(leave)={acts['keep']}   "
              f"ARCHIVE(drop INBOX)={acts['archive']}")
        for r in [x for x in rows if x["action"] == "archive"][:25]:
            print(f"    archive  {r['sender'][:30]:30} | {r['subject'][:46]}")

        result = {"user": args.user, "mailbox": args.mailbox, "total": len(rows),
                  "mode": "apply" if args.apply else "dry_run", "rows": rows}
        if args.apply:
            flagged = archived = ferr = aerr = 0
            unstarred = uerr = 0
            for r in rows:
                if r["action"] == "fire" and not r["is_starred"]:
                    if provider.star(r["uid"]):
                        flagged += 1
                    else:
                        ferr += 1
                elif r["action"] == "archive":
                    if provider.archive(r["uid"]):
                        archived += 1
                        r["archived"] = True
                    else:
                        aerr += 1
                        r["archived"] = False
                    # Noise leaving the inbox loses its spurious star too, so the
                    # flag pile converges with the inbox instead of stranding a
                    # star on every archived newsletter (the "257 flag storm"
                    # residue). Unstar is a \Flagged STORE — proven to work.
                    if r["is_starred"]:
                        if provider.unstar(r["uid"]):
                            unstarred += 1
                            r["unstarred"] = True
                        else:
                            uerr += 1
                            r["unstarred"] = False
            result.update(flagged=flagged, archived=archived, unstarred=unstarred,
                          flag_errors=ferr, archive_errors=aerr, unstar_errors=uerr)
            print(f"  APPLIED: flagged={flagged}  archived={archived}  "
                  f"unstarred={unstarred}  "
                  f"(errors: flag={ferr} archive={aerr} unstar={uerr})")
        else:
            print("  DRY RUN — no changes. Re-run with --apply to execute.")

        # Residual-star sweep: unstar noise that is starred but already out of the
        # inbox, so the flag pile fully drains (not just the inbox-resident stars).
        if args.sweep_starred:
            result["starred_sweep"] = sweep_starred_noise(
                provider, args.limit, args.apply)

        receipt = args.receipt or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "audit",
            f"imap_sweep-{args.user.replace('@', '_at_')}.json")
        os.makedirs(os.path.dirname(receipt), exist_ok=True)
        with open(receipt, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"  receipt → {receipt}")
        return 0
    finally:
        provider.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
