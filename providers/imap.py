"""
IMAP provider implementation.

Supports standard IMAP servers and Gmail IMAP with X-GM-LABELS extension.
"""

import email
import imaplib
import logging
import os
import ssl
import subprocess
from email.header import decode_header
from typing import Any, Dict, List, Optional, Tuple

from providers.base import (
    EmailProvider,
    ProviderCapabilities,
    ListMessagesResult,
)
from core.models import EmailMessage, LabelAction, ProcessingResult

logger = logging.getLogger(__name__)


def _decode_header_value(s: str) -> str:
    """Decode an email header value handling different encodings."""
    if not s:
        return ""
    decoded = decode_header(s)
    parts = []
    for text, enc in decoded:
        if isinstance(text, bytes):
            parts.append(text.decode(enc or "utf-8", errors="ignore"))
        else:
            parts.append(text)
    return " ".join(parts)


class IMAPProvider(EmailProvider):
    """
    Generic IMAP provider with optional Gmail extensions.

    Supports standard IMAP servers as well as Gmail's IMAP implementation
    with X-GM-LABELS for true label operations.

    Configuration via environment variables:
        IMAP_HOST: IMAP server hostname (default: imap.gmail.com)
        IMAP_USER: Username/email address
        IMAP_PASS: Password or app-specific password
        OP_ACCOUNT, OP_ITEM, OP_FIELD: 1Password integration for password

    Example:
        provider = IMAPProvider(
            host="imap.gmail.com",
            user="user@gmail.com",
            password="app-password",  # allow-secret
            use_gmail_extensions=True,
        )
        with provider:
            result = provider.list_messages("ALL", limit=100)
    """

    name = "imap"

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,  # allow-secret
        use_gmail_extensions: bool = False,
        port: int = 993,
    ):
        """
        Initialize IMAP provider.

        Args:
            host: IMAP server hostname (or IMAP_HOST env var)
            user: Username (or IMAP_USER env var)
            password: Password (or IMAP_PASS/1Password env vars)  # allow-secret
            use_gmail_extensions: Enable Gmail-specific IMAP extensions
            port: IMAP port (default 993 for SSL)
        """
        self.host = host or os.getenv("IMAP_HOST", "imap.gmail.com")
        self.user = user or os.getenv("IMAP_USER")
        self._password = password
        self.use_gmail_extensions = use_gmail_extensions
        self.port = port
        self._connection: Optional[imaplib.IMAP4_SSL] = None
        self._created_folders: set = set()
        self._current_mailbox: Optional[str] = None

        # Set capabilities based on mode
        self.capabilities = ProviderCapabilities.FOLDERS | ProviderCapabilities.SEARCH_QUERY
        if use_gmail_extensions:
            self.capabilities |= ProviderCapabilities.GMAIL_EXTENSIONS
            self.capabilities |= ProviderCapabilities.TRUE_LABELS
            self.capabilities |= ProviderCapabilities.STAR

    def _load_password(self) -> str:
        """Load password from environment or 1Password."""
        if self._password:
            return self._password

        if os.getenv("IMAP_PASS"):
            return os.getenv("IMAP_PASS")

        # Try 1Password CLI
        op_account = os.getenv("OP_ACCOUNT")
        op_item = os.getenv("OP_ITEM")
        op_field = os.getenv("OP_FIELD", "password")

        if op_account and op_item:
            try:
                result = subprocess.check_output(
                    ["op", "item", "get", op_item, "--account", op_account, f"--field={op_field}"],
                    text=True,
                ).strip()
                return result
            except Exception as e:
                logger.warning(f"Failed to load password from 1Password: {e}")

        raise ValueError(
            "IMAP password not configured. Set IMAP_PASS or use 1Password env vars "
            "(OP_ACCOUNT, OP_ITEM, OP_FIELD)"
        )

    def connect(self) -> None:
        """Establish IMAP connection with SSL."""
        if self._connection:
            return

        if not self.user:
            raise ValueError("IMAP_USER not configured")

        password = self._load_password()  # allow-secret

        ctx = ssl.create_default_context()
        self._connection = imaplib.IMAP4_SSL(self.host, port=self.port, ssl_context=ctx)
        self._connection.login(self.user, password)
        logger.info(f"IMAP connected to {self.host} as {self.user}")

    def disconnect(self) -> None:
        """Close IMAP connection."""
        if self._connection:
            try:
                self._connection.logout()
            except Exception as e:
                logger.debug(f"Error during IMAP logout: {e}")
            finally:
                self._connection = None
                self._current_mailbox = None
        logger.debug("IMAP disconnected")

    def _select_mailbox(self, mailbox: str = "INBOX") -> None:
        """Select a mailbox if not already selected."""
        if self._current_mailbox != mailbox:
            res, _ = self._connection.select(mailbox)
            if res != "OK":
                raise RuntimeError(f"Failed to select mailbox: {mailbox}")
            self._current_mailbox = mailbox

    def list_messages(
        self,
        query: str = "ALL",
        limit: int = 100,
        page_token: Optional[str] = None,
        mailbox: str = "INBOX",
    ) -> ListMessagesResult:
        """
        List messages matching IMAP search criteria.

        Args:
            query: IMAP SEARCH criteria (e.g., "ALL", "UNSEEN", "FROM example.com")
            limit: Maximum messages to return
            page_token: Start offset as string (for pagination)
            mailbox: Mailbox to search in (default INBOX)

        Returns:
            ListMessagesResult with message UIDs
        """
        self._select_mailbox(mailbox)

        res, data = self._connection.uid("search", None, query)
        if res != "OK":
            raise RuntimeError(f"IMAP search failed: {query}")

        uids = data[0].split() if data[0] else []
        total = len(uids)

        # Handle pagination via offset
        start = int(page_token) if page_token else 0
        # Take most recent first (reverse order)
        uids = uids[max(0, total - start - limit):total - start]

        messages = []
        for uid in uids:
            messages.append(EmailMessage(
                id=uid.decode() if isinstance(uid, bytes) else str(uid),
                sender="",
                subject="",
            ))

        # Calculate next page token
        next_start = start + limit
        next_token = str(next_start) if next_start < total else None

        return ListMessagesResult(
            messages=messages,
            next_page_token=next_token,
            total_estimate=total,
        )

    def get_message_details(self, message_id: str) -> Optional[EmailMessage]:
        """Fetch message headers by UID."""
        res, data = self._connection.uid(
            "fetch",
            message_id,
            "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])",
        )
        if res != "OK" or not data or data[0] is None:
            return None

        msg = email.message_from_bytes(data[0][1])
        sender = _decode_header_value(msg.get("From", ""))
        subject = _decode_header_value(msg.get("Subject", ""))

        # Get flags/labels
        labels = set()
        is_starred = False
        is_read = False

        if self.use_gmail_extensions:
            # Fetch Gmail labels
            res, label_data = self._connection.uid("fetch", message_id, "(X-GM-LABELS)")
            if res == "OK" and label_data and label_data[0]:
                # Parse X-GM-LABELS response
                raw = label_data[0][1] if isinstance(label_data[0], tuple) else label_data[0]
                if raw:
                    raw_str = raw.decode() if isinstance(raw, bytes) else str(raw)
                    # Extract labels from parentheses
                    import re
                    match = re.search(r'\(([^)]*)\)', raw_str)
                    if match:
                        for label in match.group(1).split():
                            label = label.strip('"')
                            labels.add(label)
                            if label == "\\Starred":
                                is_starred = True

        # Fetch flags for read status
        res, flag_data = self._connection.uid("fetch", message_id, "(FLAGS)")
        if res == "OK" and flag_data and flag_data[0]:
            raw = flag_data[0][1] if isinstance(flag_data[0], tuple) else flag_data[0]
            if raw:
                raw_str = raw.decode() if isinstance(raw, bytes) else str(raw)
                is_read = "\\Seen" in raw_str
                if "\\Flagged" in raw_str:
                    is_starred = True

        return EmailMessage(
            id=message_id,
            sender=sender,
            subject=subject,
            labels=labels,
            is_starred=is_starred,
            is_read=is_read,
        )

    @staticmethod
    def _gm_label_value(label: str) -> str:
        """Format one label as an X-GM-LABELS STORE value (parenthesised list).

        Gmail *system* labels (``\\Inbox``, ``\\Starred``, ``\\Trash``, …) are
        backslash-prefixed ATOMS and must NOT be quoted: a quoted ``"\\Inbox"``
        is an invalid IMAP quoted string (``\\I`` is not a legal escape), so
        Gmail rejects the whole command with ``BAD Could not parse command``.
        That bug made ``archive()`` a silent 100 %% no-op the first time --apply
        ran against real Gmail (verified 2026-07-03: 196/196 archive_errors).
        *User* labels are arbitrary text and MUST be quoted (with any embedded
        backslash/quote escaped). Both forms go inside a parenthesised list, the
        documented X-GM-LABELS shape."""
        if label.startswith("\\"):
            return f"({label})"
        escaped = label.replace("\\", "\\\\").replace('"', '\\"')
        return f'("{escaped}")'

    def apply_label(self, message_id: str, label: str) -> bool:
        """
        Add a label to a message.

        For Gmail IMAP: Uses X-GM-LABELS extension.
        For standard IMAP: Copies message to folder.
        """
        if self.use_gmail_extensions:
            return self._checked_store(
                message_id, "+X-GM-LABELS", self._gm_label_value(label),
                f"apply Gmail label {label}")
        else:
            # Standard IMAP: copy to folder
            self.ensure_label_exists(label)
            try:
                res, _ = self._connection.uid("COPY", message_id, f'"{label}"')
                return res == "OK"
            except Exception as e:
                logger.error(f"Failed to copy to folder {label}: {e}")
                return False

    def _checked_store(self, message_id: str, op: str, value: str,
                       what: str) -> bool:
        """Issue ``UID STORE`` and return True ONLY on an ``OK`` response.

        imaplib raises only on ``BAD``; a server ``NO`` (label missing,
        permission, quota, read-only mailbox, invalid flag) comes back as a
        normal ``('NO', ...)`` tuple. Returning True unconditionally reported
        those rejections as success and they entered the audit as applied
        (review U085) — so every flag/label mutation routes through here."""
        try:
            res, _ = self._connection.uid("STORE", message_id, op, value)
            if res != "OK":
                logger.error(
                    f"Failed to {what}: STORE returned {res} for {message_id}")
                return False
            return True
        except Exception as e:
            logger.error(f"Failed to {what}: {e}")
            return False

    def _server_supports(self, capability: str) -> bool:
        """True iff the connected IMAP server advertised ``capability``.
        imaplib populates ``connection.capabilities`` (a tuple of upper-case
        names) at connect; match by exact token so e.g. ``UIDPLUS`` never
        misfires on a bare ``UID``. Note this is the SERVER's capability set —
        distinct from ``self.capabilities`` (the ProviderCapabilities flags)."""
        conn = self._connection
        caps = getattr(conn, "capabilities", None) if conn else None
        if not caps:
            return False
        return capability.upper() in {str(c).upper() for c in caps}

    def remove_label(self, message_id: str, label: str) -> bool:
        """
        Remove a label from a message.

        For Gmail IMAP: Uses X-GM-LABELS extension.
        For standard IMAP: Not directly supported (would need move).
        """
        if self.use_gmail_extensions:
            # The STORE result must be honoured: a NO means the label removal
            # was rejected, so we must NOT report success (review U086) — else
            # archive() would record the message as having left the inbox.
            return self._checked_store(
                message_id, "-X-GM-LABELS", self._gm_label_value(label),
                f"remove Gmail label {label}")
        else:
            logger.warning("remove_label not supported for standard IMAP (folder-based)")
            return False

    def _gmail_archive(self, message_id: str) -> bool:
        """Archive a Gmail message: remove it from INBOX, KEEP it in All Mail.

        Gmail does NOT expose ``\\Inbox`` in X-GM-LABELS, so
        ``-X-GM-LABELS \\Inbox`` returns OK but archives NOTHING — a silent
        no-op verified live 2026-07-03 (store OK, message still in inbox; probed
        methods A/B both no-op, C/E work). The real primitive is ``+FLAGS
        \\Deleted`` + a UID-scoped EXPUNGE while INBOX is selected: Gmail treats
        an expunge from a label view as "drop that label", so the message leaves
        the inbox but survives in All Mail (reversible; never deleted).

        SAFETY — this is a footgun. From ``[Gmail]/All Mail`` or Trash the same
        two commands PERMANENTLY delete. So refuse unless INBOX is the selected
        mailbox, and require UIDPLUS so the EXPUNGE is scoped to THIS uid, never
        mailbox-wide. On EXPUNGE failure, roll the ``\\Deleted`` flag back so a
        message is never left hidden-but-present."""
        if self._current_mailbox != "INBOX":
            logger.error(
                "gmail archive refused: selected mailbox is %r, not INBOX — a "
                "\\Deleted+EXPUNGE there would delete, not archive.",
                self._current_mailbox)
            return False
        # NB: we intentionally do NOT gate on _server_supports("UIDPLUS"). Gmail
        # always supports UIDPLUS and honours a UID-scoped EXPUNGE (proven live
        # 2026-07-03 by a probe on the real mailbox), but imaplib's capability
        # tuple does not reliably list UIDPLUS after Gmail login, so the check
        # returned False and refused every archive (archived=0, archive_errors=
        # 193). use_gmail_extensions already implies Gmail, so the scoped
        # ``UID EXPUNGE`` below is safe; the load-bearing guard is the INBOX-only
        # check above (which prevents a delete from All Mail/Trash).
        if not self._checked_store(message_id, "+FLAGS", r"(\Deleted)",
                                   "flag \\Deleted for gmail archive"):
            return False
        try:
            res, _ = self._connection.uid("EXPUNGE", message_id)
        except Exception as e:
            res = None
            logger.error(f"gmail archive: scoped EXPUNGE raised for {message_id}: {e}")
        if res != "OK":
            # Never leave a message flagged \Deleted-but-present (clients hide it).
            self._connection.uid("STORE", message_id, "-FLAGS", r"(\Deleted)")
            logger.error(
                f"gmail archive: scoped EXPUNGE returned {res} for {message_id}; "
                "rolled back \\Deleted — not archived.")
            return False
        return True

    def archive(self, message_id: str) -> bool:
        """Archive a message.

        Gmail extensions: remove from INBOX via ``\\Deleted`` + a UID-scoped
        EXPUNGE (message stays in All Mail). ``-X-GM-LABELS \\Inbox`` is a silent
        no-op on Gmail — see _gmail_archive.

        Standard IMAP: relocate to the Archive folder, reporting success ONLY
        when the message actually LEFT the source mailbox — via atomic UID MOVE
        (RFC 6851), or COPY + ``\\Deleted`` + scoped UID EXPUNGE (RFC 4315 /
        UIDPLUS). It NEVER issues a mailbox-wide ``expunge()`` (which would
        destroy unrelated ``\\Deleted`` mail), and it never reports success for a
        message merely flagged ``\\Deleted``-but-still-present: clients hide such
        messages so the old code looked like it archived while the caller
        recorded ``did_leave_inbox=True`` for a message still in the inbox
        (review U131)."""
        if self.use_gmail_extensions:
            return self._gmail_archive(message_id)

        try:
            # Preferred: atomic, server-side UID MOVE. Trust its result and never
            # fall through to COPY (a non-OK MOVE that did would duplicate).
            if self._server_supports("MOVE"):
                res, _ = self._connection.uid("MOVE", message_id, '"Archive"')
                return res == "OK"

            # Without MOVE we need UIDPLUS to expunge just this one message. With
            # neither, the only removal primitive is a mailbox-wide EXPUNGE, which
            # we refuse — so decline rather than leave the message copied + flagged
            # \\Deleted (a dangling duplicate) while falsely claiming success.
            if not self._server_supports("UIDPLUS"):
                logger.warning(
                    "archive: server advertises neither MOVE nor UIDPLUS; cannot "
                    "safely relocate a single message without a mailbox-wide "
                    "EXPUNGE — reporting not-archived (no changes made).")
                return False

            res, _ = self._connection.uid("COPY", message_id, '"Archive"')
            if res != "OK":
                return False  # nothing copied; original untouched
            sres, _ = self._connection.uid(
                "STORE", message_id, "+FLAGS", r"(\Deleted)")
            if sres != "OK":
                logger.error(
                    f"archive: STORE \\Deleted returned {sres} for {message_id}; "
                    "copy made but original not flagged — not archived.")
                return False
            # Scoped to THIS uid (UIDPLUS), not mailbox-wide.
            eres, _ = self._connection.uid("EXPUNGE", message_id)
            return eres == "OK"
        except Exception as e:
            logger.error(f"Failed to archive message: {e}")
            return False

    def star(self, message_id: str, due_date: Any = None) -> bool:
        """Flag/star a message. due_date is ignored (IMAP doesn't support it)."""
        return self._checked_store(
            message_id, "+FLAGS", r"(\Flagged)", "star message")

    def unstar(self, message_id: str) -> bool:
        """Remove flag/star from a message."""
        return self._checked_store(
            message_id, "-FLAGS", r"(\Flagged)", "unstar message")

    def ensure_label_exists(self, label: str) -> str:
        """Ensure folder exists, creating if necessary."""
        if label in self._created_folders:
            return label

        try:
            res, _ = self._connection.create(label)
            # OK = created, NO = already exists
            if res in ("OK", "NO"):
                self._created_folders.add(label)
        except Exception as e:
            logger.debug(f"Folder create attempt for {label}: {e}")

        return label

    def mark_read(self, message_id: str) -> bool:
        """Mark message as read."""
        return self._checked_store(
            message_id, "+FLAGS", r"(\Seen)", "mark read")

    def mark_unread(self, message_id: str) -> bool:
        """Mark message as unread."""
        return self._checked_store(
            message_id, "-FLAGS", r"(\Seen)", "mark unread")

    def append(self, message_bytes: bytes,
               mailbox: str = "[Gmail]/Drafts") -> bool:
        """Persist a raw RFC822 message into ``mailbox`` via IMAP APPEND — the keyless,
        TCC-free way to save a DRAFT. NEVER sends: APPEND only writes to a mailbox, so
        there is structurally no send path here.

        This is the headless counterpart to MailAppProvider.create_draft: instead of
        driving Apple Mail through AppleScript (which needs the one-time macOS Automation
        grant — lever L-MAIL-AUTOMATION-GRANT #960), it logs into Gmail over the
        app-password and APPENDs the message to ``[Gmail]/Drafts`` with the ``\\Draft``
        flag. Drafts is a REAL folder on Gmail (unlike the label-backed inbox), so it
        sticks reliably.

        Honesty: imaplib raises only on ``BAD``; a server ``NO`` (quota, ACL, unknown
        mailbox) comes back as a normal ``('NO', ...)`` tuple — so we report True ONLY on
        an ``OK`` response, mirroring ``_checked_store`` (review U085/U131 precedent)."""
        self.connect()  # idempotent — no-op if already connected
        payload = message_bytes if isinstance(message_bytes, bytes) else str(message_bytes).encode("utf-8")
        try:
            typ, _ = self._connection.append(mailbox, r"(\Draft)", None, payload)
        except Exception as e:
            logger.error(f"IMAP APPEND to {mailbox} failed: {e}")
            return False
        if typ != "OK":
            logger.error(f"IMAP APPEND to {mailbox} returned {typ}")
            return False
        return True

    def create_draft(self, to_addr: str, subject: str, body: str,
                     account: Optional[str] = None) -> bool:
        """Save a DRAFT (never sent) to Gmail headlessly — the keyed mirror of
        MailAppProvider.create_draft. Builds an RFC822 reply message and APPENDs it to
        ``[Gmail]/Drafts`` over the app-password; no AppleScript, no TCC grant.

        ``From`` is the authenticated mailbox (``self.user``); ``account`` (an Apple-Mail
        account NAME in the caller) is accepted for signature parity but ignored here —
        the keyed path always writes to the Gmail account it is logged into. Any ``Re:``
        prefix is added at most once. This NEVER sends (see ``append``)."""
        from email.message import EmailMessage as _Msg
        msg = _Msg()
        msg["From"] = self.user or ""
        msg["To"] = to_addr
        subj = subject or ""
        msg["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}".strip()
        msg.set_content(body or "")
        return self.append(msg.as_bytes(), "[Gmail]/Drafts")

    @staticmethod
    def _norm_subject(subject: str) -> str:
        """Strip repeated ``Re:``/``Fwd:``/``Fw:`` prefixes down to the bare
        subject stem, so an inbound message ("Foo") and its reply ("Re: Foo")
        match the same thread when searched by SUBJECT."""
        s = (subject or "").strip()
        low = s.lower()
        while True:
            for p in ("re:", "fwd:", "fw:"):
                if low.startswith(p):
                    s = s[len(p):].strip()
                    low = s.lower()
                    break
            else:
                break
        return s

    def _search_mailbox(self, mailbox: str, to_addr: str, subject: str) -> bool:
        """True iff ``mailbox`` holds ≥1 message ``TO`` ``to_addr`` whose SUBJECT
        contains the (Re:-stripped) ``subject`` stem.

        FAIL-OPEN by design: any error, non-OK response, or empty subject/addr
        returns ``False`` (→ "not handled" → the caller still drafts). Wrongly
        suppressing a genuine reply-owed draft is worse than a possible
        duplicate, so the safe default on uncertainty is to draft. SELECT is
        read-only — this method never mutates the mailbox."""
        stem = self._norm_subject(subject)
        if not to_addr or not stem:
            return False
        try:
            res, _ = self._connection.select(mailbox, readonly=True)
            if res != "OK":
                return False
            self._current_mailbox = mailbox
            typ, data = self._connection.uid(
                "search", None, "TO", f'"{to_addr}"', "SUBJECT", f'"{stem}"')
            if typ != "OK" or not data or not data[0]:
                return False
            return bool(data[0].split())
        except Exception as e:  # noqa: BLE001 — fail-open (see docstring)
            logger.debug(f"handled-check search in {mailbox} failed: {e}")
            return False

    def thread_already_handled(self, to_addr: str, subject: str,
                               sent_mailbox: Optional[str] = None,
                               drafts_mailbox: str = "[Gmail]/Drafts") -> bool:
        """Server-truth reconciliation: is this reply-owed thread ALREADY handled?

        Returns True iff a reply to ``to_addr`` with this subject stem already
        exists in the Sent folder (the operator already answered) OR a draft
        already exists in Drafts (dedup). The draft leaf calls this before every
        APPEND to skip both already-answered threads and duplicate drafts — the
        fix for stale/triplicate drafts. It keys on the SERVER's own state, so
        it is robust where the local ``drafts_created.json`` idempotency file is
        not: a lost/reset state file, or one sender reachable at two addresses
        (which produce two different domain-keyed idempotency keys and so slip
        past local dedup — exactly how three copies of one legal reply were
        created). FAIL-OPEN throughout (see ``_search_mailbox``)."""
        self.connect()  # idempotent
        sent = sent_mailbox or os.getenv("LIMEN_MAIL_SENT_MAILBOX", "[Gmail]/Sent Mail")
        if self._search_mailbox(sent, to_addr, subject):
            return True
        return self._search_mailbox(drafts_mailbox, to_addr, subject)
