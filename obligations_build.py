#!/usr/bin/env python3
"""obligations_build.py — turn the keyless inbox-sweep receipts into the Obligations Ledger.

The north star: every buried obligation KNOWN, OWNED, and PERVASIVE — then he tends only
the handful he decides matters. This is the generator half: it reads the JSON receipts
that ``inbox_sweep.py`` already wrote for every account (audit/inbox_sweep-*.json — the
fires were classified keylessly from Apple Mail), runs each fire through the
``core.protocols`` decision cascade (protocol → precedent → exploration), collapses the
recurring senders into one owned line with a count, and emits a single
``obligations-ledger.json`` (same one-feed shape as Limen's revenue-ladder.json) that the
pervasive faces render.

No network, no LLM, no mail mutation — pure read of on-disk receipts + the derived cascade.
Fail-open: a torn or missing receipt is skipped, never fatal. Re-run any time the sweep
refreshes the receipts; the ledger is fully regenerated from provenance each time.
"""

import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.protocols import derive, _addr  # noqa: E402
from core.unsubscribe import propose as propose_unsubscribe  # noqa: E402
from draft_writer import _ob_key  # noqa: E402

# ── Inbound-lead SAFE opt-in ────────────────────────────────────────────────────────────
# The ONLY two obligation classes stamped with a `safe_intent` (which the fail-closed
# send_drafts leaf reads to auto-send a bracket-free registry template WHEN ARMED). Each maps
# to a registry intent id that must exist in limen/institutio/governance/mail-tiers.yaml.
# NEVER inbound-linkedin — its senders are noreply relays (structurally unsendable, correct).
_INBOUND_SAFE_INTENT = {
    "inbound-lead-hire": "inbound-ack-hire",
    "inbound-lead-deploy": "inbound-ack-deploy",
}
# Class-scoped blast-radius cap: at most this many first-touch stamps per class per build run,
# so a classifier bug can never mass-arm an auto-send. Combined with the drafts_sent.json
# first-touch check (a lead is stamped once, then never again once its ack has fired), the
# armed set stays bounded and single-fire.
_INBOUND_SAFE_CAP_PER_CLASS = 5


def _load_sent_keys(receipts_dir):
    """First-touch signal, offline: the set of _ob_keys whose SAFE ack has already been sent
    (audit/drafts_sent.json, written by send_drafts.py). A key present ⇒ NOT first-touch ⇒ do
    not re-stamp. Pure on-disk read — preserves the builder's no-network/no-LLM guarantee where
    the IMAP thread_already_handled() server-truth check (draft_writer) cannot reach. The state
    file lives beside the receipts (audit/); missing/torn ⇒ empty set (fail-open → treat as
    first-touch, but the ≤5 cap still bounds the blast radius)."""
    path = os.path.join(receipts_dir, "..", "audit", "drafts_sent.json")
    # receipts_dir defaults to <repo>/audit, so drafts_sent.json usually sits IN it; accept both.
    for cand in (os.path.join(receipts_dir, "drafts_sent.json"), path):
        try:
            data = json.loads(open(cand).read())
            if isinstance(data, list):
                return set(data)
        except (OSError, ValueError):
            continue
    return set()

# Human-facing phrase per protocol class (the derived title prefix).
_CLS_TITLE = {
    "security-credential-change": "Security — credential change",
    "fraud-alert": "Fraud alert — verify first",
    "loan-default": "Student loan — default risk",
    "billing-decline": "Billing — payment failed",
    "kyc": "KYC / identity verification",
    "inbound-lead-hire": "Warm lead — hire door",
    "inbound-lead-deploy": "Warm lead — deploy door",
    "inbound-linkedin": "LinkedIn inbound — needs email path",
    "legal-sign": "Legal — document to sign",
    "legal-correspondence": "Legal — correspondence",
    "registered-agent": "Registered agent / LLC",
    "subscription-renewal": "Subscription renewal",
    "domain-renewal": "Domain renewal",
    "infra-alarm": "Infra alarm (self)",
    "app-update": "App update",
    "precedent": "Personal — reply owed",
    "exploration": "Needs review",
    "bulk": "Bulk / list mail (no reply owed)",
}

# His-hand levers — surfaced as known/owned (never forced). Mirrors the cascade memory.
_LEVERS = [
    {"id": "L-MCP", "label": "Expand the Gmail MCP connector to write scope (gmail.modify) "
     "in claude.ai — one-time, Anthropic-managed refresh; unlocks reliable keyless Gmail "
     "archive + drafts in any live session.", "owner": "yours", "cost": "~30s, durable"},
    {"id": "L-IMAP-APP-PW", "label": "Generate a Google app-password (account.google.com → "
     "Security → App passwords) — unlocks reliable Gmail archive/draft over direct IMAP for "
     "the headless daemon; no weekly token death, no browser-driving.", "owner": "yours",
     "cost": "~60s once, durable"},
    {"id": "L-OAUTH", "label": "Revive the Gmail OAuth app: fresh consent + flip to "
     "Production publishing (stops the 7-day testing-mode token expiry). I can browser-drive "
     "the consent when you want it.", "owner": "yours", "cost": "a few min, durable"},
    {"id": "LIMEN_NTFY_TOPIC", "label": "Set LIMEN_NTFY_TOPIC to get high-priority "
     "obligations pushed to your phone (opt-in; no topic = no push).", "owner": "yours",
     "cost": "30s"},
]

_SPINE = ("every buried obligation — known, owned, one tap. mail is the first feed; "
          "you tend only the handful you decide matters.")


def _sender_name(sender: str) -> str:
    name = (sender or "").split("<")[0].strip().strip('"')
    if name and "@" not in name:
        return name
    dom = _addr(sender).split("@")[-1]
    return dom or "(unknown sender)"


def _domain(sender: str) -> str:
    return _addr(sender).split("@")[-1] or "(none)"


def load_receipts(receipts_dir):
    """Yield (account, result, rows) for every readable receipt. Fail-open per file."""
    for path in sorted(glob.glob(os.path.join(receipts_dir, "inbox_sweep-*.json"))):
        try:
            data = json.loads(open(path).read())
        except (OSError, ValueError):
            continue
        result = data.get("result", {})
        account = result.get("account", os.path.basename(path))
        yield account, result, data.get("rows", [])


def build(receipts_dir):
    accounts = []
    archive_rows = []   # for the unsubscribe propose-mode noise-killers
    sent_keys = _load_sent_keys(receipts_dir)   # first-touch signal for inbound SAFE stamping
    # collapse recurring: key = (class, sender-domain) → aggregate
    agg = {}
    for account, result, rows in load_receipts(receipts_dir):
        fires = [r for r in rows
                 if r.get("action") == "fire" or r.get("is_flagged")]
        for r in rows:
            if r.get("action") == "archive":
                archive_rows.append({**r, "_account": account})
        accounts.append({
            "account": account,
            "total": result.get("total", len(rows)),
            "fires": len(fires),
            "archived": result.get("archived", result.get("archive_requested", 0)),
        })
        for r in fires:
            sender = r.get("sender", "")
            subject = r.get("subject", "")
            snippet = r.get("snippet") or r.get("body") or r.get("summary") or ""
            # Bulk-mail headers (List-Unsubscribe / List-Id / Precedence: bulk) suppress the
            # personal reply-owed precedent rung. Accept whichever header field the receipt
            # carries (raw block or mapping); absent → no suppression (fail-open).
            headers = r.get("headers") or r.get("raw_headers")
            ob = derive(sender, subject, r.get("label", ""), r.get("tier", 4), snippet,
                        headers=headers)
            # Reply-To (captured by inbox_sweep into the receipt row): a draft/reply should
            # prefer it over the raw From (InMail relays thread back through a reply-* address).
            reply_to = (r.get("reply_to") or "").strip()
            key = (ob.cls, _domain(sender))
            entry = agg.get(key)
            if entry is None:
                entry = {
                    "cls": ob.cls, "rung": ob.rung, "priority": ob.priority,
                    "title": f"{_CLS_TITLE.get(ob.cls, ob.cls)} — {_sender_name(sender)}",
                    "sender": sender, "reply_to": reply_to or None,
                    "domain": _domain(sender), "accounts": set(),
                    "next_step": ob.next_step, "why": ob.why, "owner": ob.owner,
                    "verify_first": ob.verify_first, "requires_reply": ob.requires_reply,
                    "draft_hint": ob.draft_hint, "tags": list(ob.tags),
                    "occurrences": 0, "sample_subjects": [], "message_ids": [],
                }
                agg[key] = entry
            # First non-empty Reply-To wins for the collapsed line (mirrors the sender pick).
            if reply_to and not entry.get("reply_to"):
                entry["reply_to"] = reply_to
            entry["accounts"].add(account)
            entry["occurrences"] += 1
            if subject and subject not in entry["sample_subjects"]:
                entry["sample_subjects"].append(subject)
            mid = str(r.get("id", ""))
            if mid:
                entry["message_ids"].append(mid)

    obligations = []
    for e in agg.values():
        e["accounts"] = sorted(e["accounts"])
        e["sample_subjects"] = e["sample_subjects"][:4]
        obligations.append(e)

    # Stamp `safe_intent` ONLY on first-touch obligations of the two inbound-lead classes
    # (never inbound-linkedin — noreply senders, structurally unsendable). First-touch =
    # its _ob_key is not yet in drafts_sent.json (offline signal). Class-scoped cap ≤5 per
    # build run so a classifier bug can never mass-arm auto-sends. This is the ONLY place a
    # safe_intent is set; send_drafts stays fail-closed everywhere else.
    stamped_per_class = Counter()
    for o in obligations:
        intent = _INBOUND_SAFE_INTENT.get(o["cls"])
        if not intent:
            continue
        if _ob_key(o) in sent_keys:            # ack already fired ⇒ not first-touch
            continue
        if stamped_per_class[o["cls"]] >= _INBOUND_SAFE_CAP_PER_CLASS:
            continue                            # class blast-radius cap reached this run
        o["safe_intent"] = intent
        stamped_per_class[o["cls"]] += 1

    # sort by consequence, then by how loudly it's recurring
    obligations.sort(key=lambda x: (x["priority"], x["occurrences"]), reverse=True)

    by_rung = Counter(o["rung"] for o in obligations)
    by_class = Counter(o["cls"] for o in obligations)
    verify = [o for o in obligations if o["verify_first"]]
    noise_killers = propose_unsubscribe(archive_rows)

    return {
        "spine": _SPINE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "accounts": sorted(accounts, key=lambda a: a["account"]),
        "totals": {
            "obligations": len(obligations),
            "fires": sum(a["fires"] for a in accounts),
            "verify_first": len(verify),
            "noise_killers": len(noise_killers),
            "by_rung": dict(by_rung),
            "by_class": dict(by_class),
        },
        "obligations": obligations,
        "noise_killers": noise_killers,
        "levers": _LEVERS,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Obligations Ledger from inbox-sweep receipts.")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--receipts-dir", default=os.path.join(here, "audit"))
    limen_root = os.environ.get("LIMEN_ROOT", os.path.expanduser("~/Workspace/limen"))
    ap.add_argument("--out", default=os.environ.get(
        "LIMEN_OBLIGATIONS_LEDGER", os.path.join(limen_root, "obligations-ledger.json")))
    args = ap.parse_args(argv)

    ledger = build(args.receipts_dir)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(ledger, f, indent=2)

    t = ledger["totals"]
    print(f"obligations-ledger: {t['obligations']} obligations from {t['fires']} fires "
          f"across {len(ledger['accounts'])} accounts "
          f"({t['verify_first']} verify-first) → {args.out}")
    top = ledger["obligations"][:8]
    for o in top:
        vf = " [VERIFY]" if o["verify_first"] else ""
        print(f"  [{o['priority']:>2}] {o['cls']:<26} x{o['occurrences']:<2} {o['title'][:54]}{vf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
