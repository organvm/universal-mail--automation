#!/usr/bin/env python3
"""mail-send — the INTERACTIVE, headless CLI send lane (Gmail, keyed SMTP).

This is NOT the autonomic sender. `send_drafts.py` is the beat's batch lane and stays
tier-locked behind LIMEN_MAIL_SEND + the mail-tiers registry; it decides for itself and
therefore must be disarmed by default. THIS tool sends exactly what a human (or a session
acting on a human's explicit ask) invokes it with — the invocation IS the authorization,
so there is no tier gate here. It is never wired into the beat.

Every send is server-verified: after SMTP accepts, the tool polls [Gmail]/Sent Mail for
the outgoing Message-ID and prints a loud VERIFIED/UNVERIFIED verdict (non-zero exit if
unverified). Fail-closed everywhere else: missing credentials, invalid recipients, and
missing/oversized attachments refuse the send.

Modes
    --to A --subject S --body-file F [--cc B --bcc C --attach P ...]   compose + send
    --reply-to-search "fragment or <message-id>" --body-file F         threaded reply
        (locates the newest matching message in [Gmail]/All Mail, sets In-Reply-To/
         References, Re:-prefixes the subject, and defaults --to to the original sender)
    --from-draft "subject fragment"                                    send existing draft
        (fetches the newest matching [Gmail]/Drafts message, sends it VERBATIM over SMTP
         — Bcc header stripped from the wire copy — then moves the draft to [Gmail]/Trash)
    --self-test                                                        end-to-end predicate
        (sends a stamped message to the authenticated address and verifies it in Sent;
         exit 0 ⟺ the whole lane works)
    --dry-run                                                          print, send nothing

Credentials come from the env the credential organ hydrates (GMAIL_USER/GMAIL_APP_PASSWORD
or IMAP_USER/IMAP_PASS) — see limen scripts/creds-hydrate.py lane "gmail (C_MAIL
app-password)" and the fallback file ~/.config/mail_automation/credentials.env.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import logging
import os
import smtplib
import sys
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parseaddr

from send_drafts import _attach, _smtp_creds, classify_attachments

logger = logging.getLogger("mail_send")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
IMAP_HOST = os.environ.get("IMAP_HOST", "imap.gmail.com")
ALL_MAIL = "[Gmail]/All Mail"
DRAFTS = os.environ.get("LIMEN_MAIL_DRAFTS_MAILBOX", "[Gmail]/Drafts")
SENT = os.environ.get("LIMEN_MAIL_SENT_MAILBOX", "[Gmail]/Sent Mail")
TRASH = "[Gmail]/Trash"
HEADER_SCAN_WINDOW = 100  # newest N messages scanned per mailbox for a match

EXIT_OK = 0
EXIT_UNVERIFIED = 2
EXIT_FAIL_CLOSED = 3
EXIT_NOT_FOUND = 4


def _decode_subject(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:  # noqa: BLE001 — a mangled header should not kill a scan
        return raw


class GmailImap:
    """Thin, purpose-built IMAP helper (search newest / fetch raw / verify Sent / trash draft).

    Deliberately separate from providers.imap.IMAPProvider, whose surface is shaped around
    the obligations pipeline; this one serves the interactive send lane only.
    """

    def __init__(self, creds: tuple[str, str], host: str = IMAP_HOST):
        self._user, self._pw = creds
        self._host = host
        self._conn: imaplib.IMAP4_SSL | None = None

    def _connection(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            conn = imaplib.IMAP4_SSL(self._host)
            conn.login(self._user, self._pw)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None

    def _uid_window(self, conn: imaplib.IMAP4_SSL) -> list[bytes]:
        typ, data = conn.uid("SEARCH", None, "ALL")
        if typ != "OK" or not data or not data[0]:
            return []
        return data[0].split()[-HEADER_SCAN_WINDOW:]

    def newest_matching(self, mailbox: str, query: str) -> tuple[bytes, dict] | None:
        """Newest message in `mailbox` matching `query`.

        A query shaped like an RFC Message-ID (<...@...>) matches the Message-ID header
        exactly (server-side SEARCH); anything else is a case-insensitive substring match
        against the decoded Subject of the newest HEADER_SCAN_WINDOW messages (unicode-safe
        — IMAP SUBJECT SEARCH is ASCII-only, so we match client-side).
        Returns (uid, headers dict) or None.
        """
        conn = self._connection()
        typ, _ = conn.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            return None
        if query.startswith("<") and query.endswith(">") and "@" in query:
            typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", query)
            uids = data[0].split() if typ == "OK" and data and data[0] else []
        else:
            uids = []
            frag = query.casefold()
            for uid in reversed(self._uid_window(conn)):
                hdr = self._fetch_headers(conn, uid)
                if hdr and frag in _decode_subject(hdr.get("Subject")).casefold():
                    uids = [uid]
                    break
        if not uids:
            return None
        uid = uids[-1]
        hdr = self._fetch_headers(conn, uid)
        return (uid, hdr) if hdr else None

    def _fetch_headers(self, conn: imaplib.IMAP4_SSL, uid: bytes) -> dict | None:
        typ, data = conn.uid(
            "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID REFERENCES FROM TO DATE)])"
        )
        if typ != "OK" or not data or data[0] is None:
            return None
        raw = b""
        for part in data:
            if isinstance(part, tuple) and len(part) > 1:
                raw = part[1]
                break
        msg = email.message_from_bytes(raw)
        return {k: msg.get(k) for k in ("Subject", "Message-ID", "References", "From", "To", "Date")}

    def fetch_raw(self, mailbox: str, uid: bytes) -> bytes | None:
        conn = self._connection()
        typ, _ = conn.select(f'"{mailbox}"', readonly=True)
        if typ != "OK":
            return None
        typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ != "OK" or not data:
            return None
        for part in data:
            if isinstance(part, tuple) and len(part) > 1:
                return part[1]
        return None

    def sent_has(self, message_id: str, timeout_s: int = 60, step_s: int = 5) -> bool:
        """Poll [Gmail]/Sent Mail for `message_id` — the server-truth send verification."""
        deadline = time.monotonic() + timeout_s
        while True:
            conn = self._connection()
            typ, _ = conn.select(f'"{SENT}"', readonly=True)
            if typ == "OK":
                typ, data = conn.uid("SEARCH", "HEADER", "Message-ID", message_id)
                if typ == "OK" and data and data[0]:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(step_s)

    def trash_draft(self, uid: bytes) -> bool:
        """Move a Drafts message to [Gmail]/Trash (reversible for ~30 days)."""
        conn = self._connection()
        typ, _ = conn.select(f'"{DRAFTS}"')
        if typ != "OK":
            return False
        typ, _ = conn.uid("COPY", uid, f'"{TRASH}"')
        if typ != "OK":
            return False
        conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
        conn.expunge()
        return True


def _validate_recipients(addrs: list[str]) -> list[str]:
    """Fail-closed recipient validation; returns problems (empty ⇒ ok)."""
    problems = []
    for a in addrs:
        addr = parseaddr(a)[1]
        if "@" not in addr:
            problems.append(f"not an address: {a!r}")
        elif addr.lower().endswith("privaterelay.appleid.com"):
            problems.append(f"private-relay address refused: {addr}")
    return problems


def _split_addrs(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out.extend(a.strip() for a in v.split(",") if a.strip())
    return out


def build_message(
    creds: tuple[str, str],
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list | None = None,
    reply_headers: dict | None = None,
) -> EmailMessage:
    """Compose the outgoing RFC822 message; sets a Message-ID so the send can be verified."""
    user, _ = creds
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)
    if reply_headers:
        orig_id = reply_headers.get("Message-ID")
        if orig_id:
            msg["In-Reply-To"] = orig_id
            prior = (reply_headers.get("References") or "").split()
            msg["References"] = " ".join(prior + [orig_id]).strip()
        orig_subject = _decode_subject(reply_headers.get("Subject")) or subject
        subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)
    _attach(msg, attachments)
    return msg


def smtp_send(msg: EmailMessage, creds: tuple[str, str], to_addrs: list[str] | None = None) -> bool:
    user, pw = creds
    wire = msg
    if "Bcc" in msg:  # never transmit the Bcc header itself
        wire = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
        del wire["Bcc"]
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.login(user, pw)
            if to_addrs:
                s.send_message(wire, from_addr=user, to_addrs=to_addrs)
            else:
                s.send_message(wire)
        return True
    except Exception as e:  # noqa: BLE001 — CLI reports and exits nonzero; never a stack trace at him
        print(f"mail-send: SMTP send failed: {e}", file=sys.stderr)
        return False


def send_and_verify(
    msg: EmailMessage,
    creds: tuple[str, str],
    imap: GmailImap,
    verify_timeout: int,
    to_addrs: list[str] | None = None,
) -> int:
    if not smtp_send(msg, creds, to_addrs=to_addrs):
        return EXIT_FAIL_CLOSED
    msgid = msg["Message-ID"]
    print(f"mail-send: SMTP accepted ({msgid})")
    if imap.sent_has(msgid, timeout_s=verify_timeout):
        print(f"mail-send: VERIFIED in {SENT}: {msg['Subject']}")
        return EXIT_OK
    print(
        f"mail-send: UNVERIFIED — SMTP accepted but {msgid} not visible in {SENT} "
        f"within {verify_timeout}s. Check the mailbox before retrying (a retry may double-send).",
        file=sys.stderr,
    )
    return EXIT_UNVERIFIED


def run_from_draft(fragment: str, creds: tuple[str, str], imap: GmailImap, verify_timeout: int, dry_run: bool) -> int:
    hit = imap.newest_matching(DRAFTS, fragment)
    if not hit:
        print(f"mail-send: no draft in {DRAFTS} matching {fragment!r}", file=sys.stderr)
        return EXIT_NOT_FOUND
    uid, _hdr = hit
    raw = imap.fetch_raw(DRAFTS, uid)
    if not raw:
        print("mail-send: draft fetch failed", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    rcpts = [a for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Bcc", []))]
    problems = _validate_recipients(rcpts)
    if not rcpts or problems:
        print(f"mail-send: draft recipients invalid: {problems or 'none found'}", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    if not msg.get("Message-ID"):
        msg["Message-ID"] = make_msgid()
    if dry_run:
        print(f"DRY-RUN — would send draft verbatim to {rcpts}:\n")
        print(msg.as_string()[:4000])
        return EXIT_OK
    rc = send_and_verify(msg, creds, imap, verify_timeout, to_addrs=rcpts)
    if rc == EXIT_OK:
        if imap.trash_draft(uid):
            print(f"mail-send: draft moved to {TRASH}")
        else:
            print("mail-send: WARNING — sent, but the draft copy could not be trashed; remove it by hand")
    return rc


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(
        prog="mail-send",
        description="Interactive headless Gmail send (keyed SMTP) with built-in Sent verification.",
    )
    ap.add_argument("--to", action="append", help="recipient (repeatable or comma-separated)")
    ap.add_argument("--cc", action="append")
    ap.add_argument("--bcc", action="append")
    ap.add_argument("--subject")
    ap.add_argument("--body-file", help="file containing the plain-text body (never inline)")
    ap.add_argument("--attach", action="append", help="attachment path (repeatable)")
    ap.add_argument(
        "--reply-to-search",
        metavar="QUERY",
        help="thread as a reply to the newest [Gmail]/All Mail message matching a subject fragment or <Message-ID>",
    )
    ap.add_argument("--from-draft", metavar="FRAGMENT", help="send the newest matching [Gmail]/Drafts message verbatim, then trash it")
    ap.add_argument("--self-test", action="store_true", help="send a stamped message to self and verify in Sent (exit 0 ⟺ lane works)")
    ap.add_argument("--dry-run", action="store_true", help="print the message; transmit nothing")
    ap.add_argument("--verify-timeout", type=int, default=60, help="seconds to wait for the Sent-mailbox verification (default 60)")
    args = ap.parse_args(argv)

    creds = _smtp_creds()
    if not creds:
        print(
            "mail-send: no credentials (GMAIL_USER/GMAIL_APP_PASSWORD or IMAP_USER/IMAP_PASS). "
            "Hydrate via limen scripts/creds-hydrate.py --apply --op, or source "
            "~/.config/mail_automation/credentials.env",
            file=sys.stderr,
        )
        return EXIT_FAIL_CLOSED

    imap = GmailImap(creds)
    try:
        if args.from_draft:
            return run_from_draft(args.from_draft, creds, imap, args.verify_timeout, args.dry_run)

        if args.self_test:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            to = [creds[0]]
            subject = f"mail-send self-test {stamp}"
            body = (
                "This is the mail-send lane self-test.\n"
                f"Sent {stamp} via SMTP ({SMTP_HOST}:{SMTP_PORT}), verified against {SENT}.\n"
                "Exit 0 from this run means the headless CLI send lane works end to end.\n"
            )
            msg = build_message(creds, to, subject, body)
            if args.dry_run:
                print(msg.as_string())
                return EXIT_OK
            return send_and_verify(msg, creds, imap, args.verify_timeout)

        to = _split_addrs(args.to)
        cc = _split_addrs(args.cc)
        bcc = _split_addrs(args.bcc)
        reply_headers = None
        if args.reply_to_search:
            hit = imap.newest_matching(ALL_MAIL, args.reply_to_search)
            if not hit:
                print(f"mail-send: no message in {ALL_MAIL} matching {args.reply_to_search!r}", file=sys.stderr)
                return EXIT_NOT_FOUND
            _uid, reply_headers = hit
            if not to:  # default the recipient to the original sender
                sender = parseaddr(reply_headers.get("From") or "")[1]
                if sender:
                    to = [sender]

        if not to:
            ap.error("--to is required (or --reply-to-search with a resolvable sender)")
        if not args.body_file:
            ap.error("--body-file is required for compose/reply sends")
        if not args.subject and not reply_headers:
            ap.error("--subject is required unless replying (--reply-to-search)")

        problems = _validate_recipients(to + cc + bcc)
        if problems:
            print("mail-send: refusing send — " + "; ".join(problems), file=sys.stderr)
            return EXIT_FAIL_CLOSED

        ok_files, oversized, missing = classify_attachments(args.attach or [])
        if oversized or missing:
            print(f"mail-send: refusing send — attachments missing={missing} oversized={oversized}", file=sys.stderr)
            return EXIT_FAIL_CLOSED

        body = open(args.body_file, encoding="utf-8").read()
        msg = build_message(
            creds, to, args.subject or "", body, cc=cc, bcc=bcc, attachments=ok_files, reply_headers=reply_headers
        )
        if args.dry_run:
            print(msg.as_string()[:4000])
            return EXIT_OK
        return send_and_verify(msg, creds, imap, args.verify_timeout, to_addrs=(to + cc + bcc) or None)
    finally:
        imap.close()


if __name__ == "__main__":
    sys.exit(main())
