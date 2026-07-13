#!/usr/bin/env python3
"""send_drafts.py — the tiered, FAIL-CLOSED auto-send leaf of the correspondence organ.

draft_writer.py produces DRAFTS and never sends (its whole design). This is the ONE place
that may SEND — and only when three independent locks all hold:

  1. ARMED    — env LIMEN_MAIL_SEND=1. Default unset ⇒ dry-run: print the plan, send nothing.
  2. SAFE     — the obligation's tier, per limen/institutio/governance/mail-tiers.yaml, is
                `safe`. no_reply and hold are NEVER sent; the hold-covers-{legal,money,
                verify_first} invariant is enforced upstream by check-mail-tiers.py.
  3. COMPLETE — the reply text is complete and bracket-free. draft_writer's `[...]` starters
                can never be sent; SAFE text comes from the registry's intent templates.

SAFE is OPT-IN, never guessed. An obligation is `safe` only if it carries an explicit
`safe_intent` naming a registry intent id — the sender never infers "this legal email is
safe." Nothing sets `safe_intent` today, so the armed sender is a correct no-op until a
class is deliberately opted in ("the armed set grows on trust"). That emptiness IS the
fail-closed guarantee, not a gap.

Idempotent: each send is recorded to audit/drafts_sent.json keyed by the same _ob_key as
drafts_created.json, checkpointed after each send, so a re-run or a crash mid-loop never
double-sends. Transport is keyed SMTP over the Gmail app-password (headless — no OAuth
scope change, no TCC grant); absent the credential it fails closed and sends nothing.

Threading note: the ledger carries no RFC Message-ID/In-Reply-To (only Apple-Mail internal
ids), so a SAFE reply is sent as a fresh `Re:` rather than threaded. Acceptable for the
low-stakes SAFE tier; anything needing true threading belongs in the HOLD (draft) tier.
"""

import argparse
import json
import os
import smtplib
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.protocols import _addr  # noqa: E402
from draft_writer import _first_name, _ob_key  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_SENT_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit", "drafts_sent.json")


def _limen_root() -> str:
    return os.environ.get("LIMEN_ROOT", os.path.expanduser("~/Workspace/limen"))


def load_tiers() -> dict | None:
    """Load the declared tier registry. Missing/unparseable ⇒ None ⇒ everything holds."""
    path = os.environ.get("LIMEN_MAIL_TIERS") or os.path.join(
        _limen_root(), "institutio", "governance", "mail-tiers.yaml")
    if yaml is None:
        return None
    try:
        data = yaml.safe_load(open(path).read())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _host(addr: str) -> str:
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _local(addr: str) -> str:
    return addr.rsplit("@", 1)[0].lower() if "@" in addr else addr.lower()


def _match_esp(host: str, patterns) -> bool:
    for pat in patterns or []:
        pat = str(pat).lower()
        if pat.endswith(".*"):                       # subdomain wildcard: "mg.*" ⇒ mg.<anything>
            if host.startswith(pat[:-1]):
                return True
        elif host == pat or host.endswith("." + pat):
            return True
    return False


def _match_role(local: str, roles) -> bool:
    return any(str(r).lower() in local for r in (roles or []))


def is_no_reply(ob: dict, tiers: dict) -> bool:
    nr = (tiers or {}).get("no_reply") or {}
    addr = _addr(ob.get("sender", ""))
    if _match_esp(_host(addr), nr.get("esp_domains")) or _match_role(_local(addr), nr.get("role_localparts")):
        return True
    # subject_patterns alone suffice for SUPPRESSION (header signals aren't in the ledger); this
    # only ever prevents an auto-send, so over-suppression fails safe (the item stays a draft).
    import re
    subj = " ".join(ob.get("sample_subjects") or [])
    for pat in nr.get("subject_patterns") or []:
        try:
            if re.search(str(pat), subj):
                return True
        except re.error:
            continue
    return False


def is_hold(ob: dict, tiers: dict) -> bool:
    hold = (tiers or {}).get("hold") or {}
    tags = set(ob.get("tags") or [])
    if tags & set(hold.get("tags") or []):
        return True
    if ob.get("cls") in set(hold.get("classes") or []):
        return True
    if hold.get("verify_first") and ob.get("verify_first"):
        return True
    return False


def safe_intent_for(ob: dict, tiers: dict) -> str | None:
    """Opt-in only: return the intent id iff the obligation explicitly names a registry intent."""
    want = ob.get("safe_intent")
    if not want:
        return None
    ids = {i.get("id") for i in ((tiers or {}).get("safe") or {}).get("intents") or []}
    return want if want in ids else None


def tier_of(ob: dict, tiers: dict) -> str:
    """Fail-closed classification: unknown ⇒ hold. hold is checked FIRST so a dangerous class
    can never be reclassified out by a no_reply/safe signal."""
    if is_hold(ob, tiers):
        return "hold"
    if is_no_reply(ob, tiers):
        return "no_reply"
    if safe_intent_for(ob, tiers):
        return "safe"
    return "hold"


def render_safe(ob: dict, tiers: dict, intent_id: str) -> str | None:
    for intent in ((tiers or {}).get("safe") or {}).get("intents") or []:
        if intent.get("id") == intent_id:
            tmpl = intent.get("template") or ""
            first = _first_name(ob.get("sender", "")) or "there"
            return tmpl.replace("{first_name}", first).strip() + "\n"
    return None


def _load_sent() -> set:
    try:
        return set(json.loads(open(_SENT_STATE).read()))
    except (OSError, ValueError):
        return set()


def _save_sent(keys) -> None:
    os.makedirs(os.path.dirname(_SENT_STATE), exist_ok=True)
    with open(_SENT_STATE, "w") as f:
        json.dump(sorted(keys), f, indent=2)


def _smtp_creds() -> tuple[str, str] | None:
    """Keyed SMTP login: (user, app-password) from the env the creds organ hydrates. None ⇒ fail closed."""
    user = os.environ.get("IMAP_USER") or os.environ.get("GMAIL_USER")
    pw = os.environ.get("IMAP_PASS") or os.environ.get("GMAIL_APP_PASSWORD")
    return (user, pw) if user and pw else None


def send_reply(to_addr: str, subject: str, body: str, creds: tuple[str, str]) -> bool:
    user, pw = creds
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001 — fail-open for the beat; never raise into the loop
        print(f"send_drafts: SMTP send failed for {to_addr}: {e}")
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tiered, fail-closed auto-send for reply-owed mail.")
    limen_root = _limen_root()
    ap.add_argument("--ledger", default=os.environ.get(
        "LIMEN_OBLIGATIONS_LEDGER", os.path.join(limen_root, "obligations-ledger.json")))
    ap.add_argument("--max", type=int, default=int(os.environ.get("LIMEN_MAIL_SEND_MAX", "10")),
                    help="cap sends per run (safety)")
    ap.add_argument("--dry-run", action="store_true", help="force dry-run even if armed")
    args = ap.parse_args(argv)

    armed = os.environ.get("LIMEN_MAIL_SEND") == "1" and not args.dry_run
    tiers = load_tiers()
    if tiers is None:
        print("send_drafts: no tier registry (mail-tiers.yaml) — everything holds; sending nothing")

    try:
        ledger = json.loads(open(args.ledger).read())
    except (OSError, ValueError) as e:
        print(f"send_drafts: cannot read ledger {args.ledger}: {e}")
        return 0  # fail-open

    sent_state = _load_sent()
    creds = _smtp_creds() if armed else None
    counts = {"hold": 0, "no_reply": 0, "safe": 0, "sent": 0, "would_send": 0, "already": 0}

    for ob in ledger.get("obligations", []):
        if not ob.get("requires_reply"):
            continue
        t = tier_of(ob, tiers)
        if t != "safe":
            counts[t] += 1
            continue
        intent = safe_intent_for(ob, tiers)
        text = render_safe(ob, tiers, intent)
        if not text or "[" in text or "]" in text:   # belt-and-suspenders: never send a starter
            counts["hold"] += 1
            continue
        counts["safe"] += 1
        to_addr = _addr(ob.get("sender", ""))
        if "@" not in to_addr or "privaterelay.appleid.com" in to_addr:
            continue
        key = _ob_key(ob)
        if key in sent_state:
            counts["already"] += 1
            continue
        subj = (ob.get("sample_subjects") or [""])[0]
        if not armed:
            counts["would_send"] += 1
            print(f"send_drafts: WOULD SEND [{intent}] → {to_addr}  (re: {subj[:60]!r})")
            continue
        if counts["sent"] >= args.max:
            print(f"send_drafts: hit --max {args.max}; stopping")
            break
        if creds is None:
            print("send_drafts: ARMED but no keyed SMTP credential (IMAP_USER/IMAP_PASS) — fail closed, sending nothing")
            break
        if send_reply(to_addr, subj or "(no subject)", text, creds):
            sent_state.add(key)
            _save_sent(sent_state)   # checkpoint AFTER each send → a crash can't double-send
            counts["sent"] += 1

    mode = "ARMED" if armed else "dry-run"
    print(f"send_drafts [{mode}]: safe={counts['safe']} sent={counts['sent']} "
          f"would_send={counts['would_send']} already={counts['already']} "
          f"held={counts['hold']} no_reply={counts['no_reply']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
