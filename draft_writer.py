#!/usr/bin/env python3
"""draft_writer.py — the outbound leaf: turn each reply-owed obligation into a ready,
voice-matched DRAFT, addressed to the sender, NEVER sent.

Two effects, both reversible and gated:
  * Always: enrich obligations-ledger.json — each obligation whose protocol owes a reply
    gets a `draft_text` (voice-matched starter) + `draft_to` (the sender's address). This
    is zero-touch: the ready draft just becomes visible in the ledger/face.
  * With --save: persist each draft to Apple Mail's Drafts folder keylessly (Drafts is a
    real folder, so it sticks even on Gmail), IDEMPOTENTLY — a draft is created at most
    once per obligation (tracked in audit/drafts_created.json), so an autonomic beat never
    piles up duplicates.

Draft-only is the entire design: there is no send, no schedule. The user always presses
send; a draft can be deleted any time. Fail-open: a compose/save error skips that one
obligation, never the rest.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.voice import load_voice_profile  # noqa: E402
from core.protocols import _addr  # noqa: E402

_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit", "drafts_created.json")


def _first_name(sender: str) -> str:
    name = (sender or "").split("<")[0].strip().strip('"')
    parts = [p for p in name.split() if p and "@" not in p and not p.isupper()]
    return parts[0] if parts else ""


def _ob_key(ob) -> str:
    """Stable idempotency key for one obligation (so re-runs never re-draft)."""
    mids = ob.get("message_ids") or []
    tail = mids[0] if mids else (ob.get("sample_subjects") or [""])[0][:40]
    return f"{ob.get('cls')}|{ob.get('domain')}|{tail}"


def compose(profile, ob, name: str) -> str:
    """A voice-matched STARTER draft (greeting + class-appropriate body + sign-off).
    Deliberately a starter with explicit [brackets] the user fills — never a fabricated
    commitment, never auto-sent."""
    sender = ob.get("sender", "")
    first = _first_name(sender)
    greeting = profile.greeting.format(first=first).strip() if first else "Hi there,"
    subj = (ob.get("sample_subjects") or [""])[0]
    hint = ob.get("draft_hint") or "Acknowledge receipt and confirm the next step."
    ref = f' regarding "{subj}"' if subj else ""
    body_lines = [
        profile.apply_style(f"Thank you for your message{ref}."),
        "",
        f"[{hint}]",
        "",
        "[Add your specifics here, then send.]",
    ]
    parts = [greeting, "", "\n".join(body_lines), "", profile.sign_off]
    sig = profile.signature or profile.name or name
    if sig:
        parts.append(sig)
    text = "\n".join(parts)
    return text.strip() + "\n"


def _select_saver(save: bool):
    """Choose the draft-save transport and return ``(save_fn, close_fn, handled_fn)``.

    Prefers the KEYED, HEADLESS IMAP APPEND path (``IMAPProvider.create_draft`` →
    ``[Gmail]/Drafts``) whenever the Gmail app-password is hydrated in the env
    (``GMAIL_APP_PASSWORD``/``IMAP_PASS`` + a user) — that path needs no macOS
    Automation grant, so it designs out lever L-MAIL-AUTOMATION-GRANT (#960). With no
    key present it FALLS BACK to the Apple-Mail AppleScript path (today's behaviour,
    unchanged). Either way nothing is ever sent — both transports only write a Draft.

    ``save_fn(to_addr, subject, body, account=None) -> bool``; ``close_fn`` (or None)
    releases the keyed connection at the end; ``handled_fn(to_addr, subject) -> bool``
    (or None) is the server-truth reconciliation check — True when a reply already
    exists in Sent or a draft already exists in Drafts, so the caller skips it (the
    fix for stale/triplicate drafts). Only the keyed IMAP path can see Sent/Drafts on
    the server, so ``handled_fn`` is None for the AppleScript and enrich-only paths.
    Returns ``(None, None, None)`` for enrich-only."""
    if not save:
        return None, None, None

    user = os.environ.get("IMAP_USER") or os.environ.get("GMAIL_USER")
    pw = os.environ.get("IMAP_PASS") or os.environ.get("GMAIL_APP_PASSWORD")
    if user and pw:
        try:
            from providers.imap import IMAPProvider
            prov = IMAPProvider(user=user, password=pw, use_gmail_extensions=True)  # allow-secret

            def _save(to_addr, subject, body, account=None):
                return prov.create_draft(to_addr, subject, body, account=account)

            def _close():
                try:
                    prov.disconnect()
                except Exception:  # noqa: BLE001 — cleanup must never raise into the beat
                    pass

            def _handled(to_addr, subject):
                return prov.thread_already_handled(to_addr, subject)

            print("draft_writer: keyed IMAP draft path (headless — no Automation grant)")
            return _save, _close, _handled
        except Exception as e:  # pragma: no cover — import/construct guard, fail-open below
            print(f"draft_writer: IMAP keyed path unavailable ({e}); falling back to Apple Mail")

    try:
        from providers.mailapp import MailAppProvider

        def _save(to_addr, subject, body, account=None):
            return MailAppProvider(account=account).create_draft(
                to_addr, subject, body, account=account)

        return _save, None, None
    except Exception as e:  # pragma: no cover
        print(f"draft_writer: MailApp unavailable ({e}); enrich-only")
        return None, None, None


def _load_state():
    try:
        return set(json.loads(open(_STATE).read()))
    except (OSError, ValueError):
        return set()


def _save_state(keys):
    os.makedirs(os.path.dirname(_STATE), exist_ok=True)
    with open(_STATE, "w") as f:
        json.dump(sorted(keys), f, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Enrich the obligations ledger with ready drafts (never sent).")
    limen_root = os.environ.get("LIMEN_ROOT", os.path.expanduser("~/Workspace/limen"))
    ap.add_argument("--ledger", default=os.environ.get(
        "LIMEN_OBLIGATIONS_LEDGER", os.path.join(limen_root, "obligations-ledger.json")))
    ap.add_argument("--name", default=os.environ.get("LIMEN_MAIL_NAME", "Anthony"))
    ap.add_argument("--save", action="store_true",
                    help="also persist drafts to Apple Mail Drafts (idempotent, never sent)")
    ap.add_argument("--max", type=int, default=int(os.environ.get("LIMEN_MAIL_DRAFTS_MAX", "12")),
                    help="cap how many drafts to persist per run (safety)")
    args = ap.parse_args(argv)

    try:
        ledger = json.loads(open(args.ledger).read())
    except (OSError, ValueError) as e:
        print(f"draft_writer: cannot read ledger {args.ledger}: {e}")
        return 0  # fail-open

    profile = load_voice_profile(name=args.name)
    obligations = ledger.get("obligations", [])
    state = _load_state()
    enriched = saved = skipped = reconciled = 0

    save_fn, close_fn, handled_fn = _select_saver(args.save)

    for ob in obligations:
        if not ob.get("requires_reply"):
            continue
        to_addr = _addr(ob.get("sender", ""))
        # only draft to a real, replyable address (skip relay/role/no-address)
        if "@" not in to_addr or "privaterelay.appleid.com" in to_addr:
            continue
        ob["draft_text"] = compose(profile, ob, args.name)
        ob["draft_to"] = to_addr
        enriched += 1

        if save_fn is not None and saved < args.max:
            key = _ob_key(ob)
            if key in state:
                ob["draft_saved"] = True
                skipped += 1
                continue
            account = (ob.get("accounts") or [None])[0]
            subj = (ob.get("sample_subjects") or [""])[0]
            re_subj = subj if subj.lower().startswith("re:") else f"Re: {subj}".strip()

            # Server-truth reconciliation (fix for stale/triplicate drafts): if a
            # reply already sits in Sent (operator answered) or a draft already
            # sits in Drafts (dedup), skip — never re-draft a handled thread.
            # Fail-open: handled_fn already swallows errors → False → we draft.
            if handled_fn is not None:
                try:
                    already = handled_fn(to_addr, subj or re_subj)
                except Exception:
                    already = False
                if already:
                    ob["already_handled"] = True
                    ob["draft_saved"] = True
                    state.add(key)
                    _save_state(state)
                    reconciled += 1
                    continue

            try:
                ok = save_fn(to_addr, re_subj or "(no subject)", ob["draft_text"], account=account)
            except Exception:
                ok = False
            if ok:
                state.add(key)
                _save_state(state)   # checkpoint AFTER each save → a crash mid-loop can't
                                     # lose the key and re-create a duplicate next beat
                ob["draft_saved"] = True
                saved += 1

    if args.save:
        _save_state(state)
        if close_fn:
            close_fn()

    with open(args.ledger, "w") as f:
        json.dump(ledger, f, indent=2)

    print(f"draft_writer: enriched {enriched} reply drafts"
          + (f"; saved {saved} new to Drafts ({skipped} already present, "
             f"{reconciled} skipped — already answered/drafted on the server)"
             if args.save else " (enrich-only)")
          + f" → {args.ledger}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
