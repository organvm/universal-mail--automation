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

KEYED SINGLE-FIRE (`--fire-obligation … --fire`, or ad-hoc `--fire-to/--fire-subject/…`):
the explicit "send THIS one" button — the real fire the operator turns for a specific reply,
including a HOLD one with PDF attachments (`--attach`). It is WHOLLY SEPARATE from the beat
loop above (which only ever auto-sends SAFE). What it may transmit is the switchable HOLD-send
boundary declared in limen's mail-tiers.yaml (`send_mode`) and overridable at runtime by
`LIMEN_MAIL_HOLD_SEND`: `safe_only` (default — SAFE only; HOLD stays in the operator's client),
`keyed_all` (any tier on an explicit --fire), or `per_matter` (HOLD only if the obligation
carries `send_ok: true`). Attachments over ~24 MiB are refused inline and named for a Drive
link (fail-closed — never a silent drop). This is why "the button doesn't exist on my side" is
false: the button is line `SMTP_SSL('smtp.gmail.com', 465)` below; the operator turns the key.
"""

import argparse
import json
import mimetypes
import os
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.protocols import _addr  # noqa: E402
from draft_writer import _first_name, _ob_key  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_SENT_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit", "drafts_sent.json")

# Keyed-fire policy. The switchable HOLD-send boundary is declared in the limen registry
# (mail-tiers.yaml -> send_mode); this mirrors its enum + fail-closed default so the sender
# degrades safely (to safe_only) if the registry is unreadable.
_SEND_MODES = ("safe_only", "keyed_all", "per_matter")
_SEND_MODE_DEFAULT = "safe_only"
# Inline-attachment ceiling: under Gmail's 25 MB hard limit once base64 (~33%) encoding
# overhead is counted. Anything larger is refused inline and named for a Drive link.
_MAX_INLINE_BYTES = 24 * 1024 * 1024


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


def _attach(msg: EmailMessage, paths) -> None:
    """Attach each file as a MIME part (type guessed; unknown ⇒ application/octet-stream).
    Callers pass ONLY files already validated to exist and fit (classify_attachments)."""
    for p in paths or []:
        fp = Path(p)
        ctype, _ = mimetypes.guess_type(fp.name)
        maintype, subtype = ctype.split("/", 1) if ctype else ("application", "octet-stream")
        msg.add_attachment(fp.read_bytes(), maintype=maintype, subtype=subtype, filename=fp.name)


def send_reply(to_addr: str, subject: str, body: str, creds: tuple[str, str], attachments=None) -> bool:
    user, pw = creds
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
    msg.set_content(body)
    _attach(msg, attachments)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001 — fail-open for the beat; never raise into the loop
        print(f"send_drafts: SMTP send failed for {to_addr}: {e}")
        return False


def resolve_send_mode(tiers: dict | None) -> str:
    """The switchable HOLD-send boundary. Precedence: env LIMEN_MAIL_HOLD_SEND override →
    registry send_mode.mode → fail-closed default 'safe_only'. An unknown value degrades to
    the default (never a wider send than declared)."""
    val = os.environ.get("LIMEN_MAIL_HOLD_SEND")
    if not val:
        val = ((tiers or {}).get("send_mode") or {}).get("mode")
    return val if val in _SEND_MODES else _SEND_MODE_DEFAULT


def mode_permits_keyed(mode: str, tier: str, ob: dict) -> bool:
    """Does send_mode permit an explicit keyed fire of a target in this tier?
      safe_only  → SAFE tier only (HOLD/ad-hoc refused; the system never transmits legal/money).
      keyed_all  → any tier on an explicit --fire.
      per_matter → SAFE, or a HOLD obligation the operator opted in with send_ok: true.
    NOTE: this gates only the EXPLICIT keyed fire; the beat auto-sender never sends non-SAFE."""
    if mode == "keyed_all":
        return True
    if tier == "safe":
        return True
    if mode == "per_matter":
        return bool((ob or {}).get("send_ok"))
    return False


def classify_attachments(paths, max_bytes: int | None = None):
    """Split attachment paths into (inlineable Paths, oversized strs, missing strs). A missing
    or oversized file makes the caller refuse the whole send (fail-closed — never silently drop
    the PDF the reply depends on). max_bytes defaults to the module ceiling (read at call time)."""
    if max_bytes is None:
        max_bytes = _MAX_INLINE_BYTES
    ok: list[Path] = []
    oversized: list[str] = []
    missing: list[str] = []
    for p in paths or []:
        fp = Path(str(p)).expanduser()
        if not fp.is_file():
            missing.append(str(p))
        elif fp.stat().st_size > max_bytes:
            oversized.append(str(p))
        else:
            ok.append(fp)
    return ok, oversized, missing


def _find_obligation(ledger_path: str, selector: str):
    """Resolve one obligation from the ledger by index, exact _ob_key, or a sender/subject
    substring (operator convenience). None if the ledger is unreadable or nothing matches."""
    try:
        ledger = json.loads(open(ledger_path).read())
    except (OSError, ValueError):
        return None
    obs = ledger.get("obligations", [])
    if selector.isdigit():
        i = int(selector)
        return obs[i] if 0 <= i < len(obs) else None
    for ob in obs:
        if _ob_key(ob) == selector:
            return ob
    needle = selector.lower()
    for ob in obs:
        hay = (ob.get("sender", "") + " " + " ".join(ob.get("sample_subjects") or [])).lower()
        if needle in hay:
            return ob
    return None


def fire_one(args, tiers: dict | None, sent_state: set) -> int:
    """The explicit keyed 'send THIS one' button — the real fire the operator turns, separate
    from the beat loop (which only ever auto-sends SAFE). Mode-gated (resolve_send_mode),
    attachment-capable, fail-closed at every step, idempotent via drafts_sent.json.

    Dry-run unless --fire (or LIMEN_MAIL_SEND=1): prints WOULD FIRE and transmits nothing."""
    mode = resolve_send_mode(tiers)

    if args.fire_obligation:
        ob = _find_obligation(args.ledger, args.fire_obligation)
        if ob is None:
            print(f"send_drafts: fire — no obligation matched {args.fire_obligation!r}; nothing sent")
            return 0
        to_addr = ob.get("draft_to") or _addr(ob.get("reply_to") or "") or _addr(ob.get("sender", ""))
        subj = (ob.get("sample_subjects") or [""])[0]
        body = ob.get("draft_text")
        if not body:
            intent = safe_intent_for(ob, tiers)
            body = render_safe(ob, tiers, intent) if intent else None
        tier = tier_of(ob, tiers)
    else:  # ad-hoc fire: an unknown target is fail-closed to the HOLD tier
        ob = {}
        to_addr = args.fire_to
        subj = args.fire_subject or "(no subject)"
        body = Path(args.fire_body_file).read_text() if args.fire_body_file else (args.fire_body or "")
        tier = "hold"

    if not to_addr or "@" not in to_addr or "privaterelay.appleid.com" in to_addr:
        print("send_drafts: fire — no valid recipient address; nothing sent")
        return 0
    if not body or "[" in body or "]" in body:
        print("send_drafts: fire — body missing or carries a '[bracket]' placeholder; refusing (fail-closed)")
        return 0

    if not mode_permits_keyed(mode, tier, ob):
        print(f"send_drafts: fire REFUSED — send_mode={mode} does not permit a {tier!r}-tier keyed fire. "
              "To arm it: set LIMEN_MAIL_HOLD_SEND=keyed_all, or (per_matter) add send_ok:true to the "
              "obligation. Nothing sent.")
        return 0

    ok, oversized, missing = classify_attachments(args.attach)
    if missing:
        print(f"send_drafts: fire — attachment(s) not found: {missing}; nothing sent")
        return 0
    if oversized:
        limit_mb = _MAX_INLINE_BYTES // (1024 * 1024)
        print(f"send_drafts: fire REFUSED — attachment(s) over the {limit_mb} MiB inline limit: {oversized}. "
              "Link them (e.g. a Drive share URL) in the body instead; nothing sent.")
        return 0

    key = _ob_key(ob) if ob else f"fire|{to_addr}|{subj}"
    if key in sent_state:
        print(f"send_drafts: fire — already sent (idempotent), skipping: {to_addr}")
        return 0

    armed = args.fire or os.environ.get("LIMEN_MAIL_SEND") == "1"
    if not armed:
        print(f"send_drafts: WOULD FIRE [{tier}] → {to_addr}  (re: {subj[:60]!r}) "
              f"+{len(ok)} attachment(s)   [add --fire to transmit]")
        return 0

    creds = _smtp_creds()
    if creds is None:
        print("send_drafts: ARMED but no keyed SMTP credential (GMAIL_USER/GMAIL_APP_PASSWORD) — "
              "fail closed, nothing sent")
        return 0

    if send_reply(to_addr, subj or "(no subject)", body, creds, attachments=ok):
        sent_state.add(key)
        _save_sent(sent_state)
        print(f"send_drafts: FIRED [{tier}] → {to_addr}  (+{len(ok)} attachment(s))")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tiered, fail-closed auto-send for reply-owed mail.")
    limen_root = _limen_root()
    ap.add_argument("--ledger", default=os.environ.get(
        "LIMEN_OBLIGATIONS_LEDGER", os.path.join(limen_root, "obligations-ledger.json")))
    ap.add_argument("--max", type=int, default=int(os.environ.get("LIMEN_MAIL_SEND_MAX", "10")),
                    help="cap sends per run (safety)")
    ap.add_argument("--dry-run", action="store_true", help="force dry-run even if armed")
    # Keyed single-fire — the explicit "send THIS one" button (mode-gated, attachment-capable).
    ap.add_argument("--fire-obligation", metavar="SELECTOR",
                    help="fire ONE ledger obligation by index / _ob_key / sender-or-subject substring")
    ap.add_argument("--fire-to", metavar="ADDR", help="ad-hoc keyed fire: recipient address")
    ap.add_argument("--fire-subject", metavar="SUBJ", help="ad-hoc keyed fire: subject")
    ap.add_argument("--fire-body-file", metavar="PATH", help="ad-hoc keyed fire: path to the reply body")
    ap.add_argument("--fire-body", metavar="TEXT", help="ad-hoc keyed fire: inline reply body")
    ap.add_argument("--attach", action="append", default=[], metavar="PATH",
                    help="attachment path for the keyed fire (repeatable; e.g. a PDF)")
    ap.add_argument("--fire", action="store_true",
                    help="actually transmit the single keyed send (else dry-run) — the explicit key-turn")
    args = ap.parse_args(argv)

    tiers = load_tiers()

    # Keyed single-fire path: the explicit operator send button, wholly separate from the beat
    # loop below (which only ever auto-sends SAFE tier). Mode-gated by send_mode / LIMEN_MAIL_HOLD_SEND.
    if args.fire_obligation or args.fire_to:
        return fire_one(args, tiers, _load_sent())

    armed = os.environ.get("LIMEN_MAIL_SEND") == "1" and not args.dry_run
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
        # Prefer Reply-To over the raw From (parity with draft_writer): a lead's reply address
        # may differ from its display From. Both normalise through _addr.
        to_addr = _addr(ob.get("reply_to") or "") or _addr(ob.get("sender", ""))
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
