"""Exact-account IMAP MOVE adapter; no COPY+global EXPUNGE fallback."""
import hashlib
import imaplib
import re

from core.flag_workflow import sha256_hex
from providers.archive_native import CONTRACT
from providers.imap_inventory import quote


class IMAPArchive:
    archive_contract = {**CONTRACT, "provider": "icloud", "mutation": "UID_MOVE"}

    def __init__(self, connection, *, account, archive_mailbox, guard):
        self.connection, self.account, self.archive, self.guard = connection, account, archive_mailbox, guard
        caps = {c.decode() if isinstance(c, bytes) else c for c in connection.capabilities}
        if "MOVE" not in caps:
            raise ValueError("server lacks UID MOVE; unsafe expunge fallback prohibited")
        if not account or archive_mailbox.casefold() == "inbox":
            raise ValueError("explicit account and distinct Archive required")
        self._select(archive_mailbox, readonly=True)

    def _select(self, mailbox, *, readonly):
        status, _ = self.connection.select(quote(mailbox), readonly=readonly)
        if status != "OK":
            raise RuntimeError("archive folder selection failed")
        _, validity = self.connection.response("UIDVALIDITY")
        if not validity or validity[0] is None:
            raise RuntimeError("archive UIDVALIDITY unavailable")
        return validity[0].decode() if isinstance(validity[0], bytes) else str(validity[0])

    def _find(self, mailbox, rfc_id, content_sha256):
        validity = self._select(mailbox, readonly=True)
        status, data = self.connection.uid("SEARCH", None, "HEADER", "Message-ID", quote(rfc_id))
        if status != "OK" or data is None:
            raise RuntimeError("archive identity search failed")
        ids = (data[0] or b"").decode().split()
        if len(ids) > 20:
            raise RuntimeError("archive identity ambiguous")
        matches = []
        for uid in ids:
            status, records = self.connection.uid("FETCH", uid, "(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.10485760>)")
            literals = [r for r in records or [] if isinstance(r, tuple)]
            if status != "OK" or len(literals) != 1:
                raise RuntimeError("archive preservation read unavailable")
            meta, raw = literals[0]
            size = re.search(rb"RFC822.SIZE (\d+)", meta)
            if size is None or len(raw) != int(size[1]):
                raise RuntimeError("archive preservation read incomplete")
            if hashlib.sha256(raw).hexdigest() == content_sha256:
                flags = sorted(f.decode() for f in imaplib.ParseFlags(meta) if f != b"\\Recent")
                matches.append({"mailbox": mailbox, "uid": uid, "uidvalidity": validity,
                                "flags": flags, "content_sha256": content_sha256})
        if len(matches) > 1:
            raise ValueError("archive exact identity has duplicate copies")
        return matches[0] if matches else None

    def observe_archive(self, mutation):
        identity = mutation["identity"]
        before = mutation["before"]
        if identity["provider"] != "icloud" or identity["account"] != self.account:
            raise ValueError("IMAP archive account mismatch")
        if mutation["destination"] != self.archive:
            raise ValueError("IMAP Archive destination mismatch")
        rfc_id, content = before["rfc_message_id"], before["content_sha256"]
        original_native = before["original_native"]
        if identity["message_id"] != sha256_hex(original_native) or original_native["mailbox"].casefold() != "inbox":
            raise ValueError("IMAP original native identity mismatch")
        if not rfc_id or not content:
            raise ValueError("full content and RFC identity required")
        source = self._find("INBOX", rfc_id, content)
        if source is not None and any(source[key] != original_native[key] for key in ("uid", "uidvalidity")):
            raise ValueError("IMAP source identity changed")
        destination = self._find(self.archive, rfc_id, content)
        if source is None and destination is None:
            raise RuntimeError("positive message preservation unavailable")
        current = source or destination
        guard = self.guard(identity)
        return {"identity": identity, "server_confirmed": True, "message_present": True,
                "in_inbox": source is not None,
                "mailboxes": (["INBOX"] if source else []) + ([self.archive] if destination else []),
                "revision": sha256_hex({"source": source, "destination": destination}),
                "location": current, "content_sha256": content, "rfc_message_id": rfc_id,
                "original_native": original_native,
                "preserved_sha256": sha256_hex({"content": content, "flags": current["flags"]}),
                "archive_destination": self.archive,
                "protected": guard["protected"], "human_override": guard["human_override"]}

    def _move(self, mutation, expected, target):
        if self.observe_archive(mutation) != expected or expected["protected"] or expected["human_override"]:
            return {"status": "not_dispatched", "reason": "fresh_observation_changed"}
        location = expected["location"]
        if self._select(location["mailbox"], readonly=False) != location["uidvalidity"]:
            return {"status": "not_dispatched", "reason": "uidvalidity_changed"}
        try:
            status, _ = self.connection.uid("MOVE", location["uid"], quote(target))
        except (OSError, imaplib.IMAP4.error):
            return {"status": "ambiguous"}
        # A tagged NO/BAD is still reconciled without automatically trying again.
        return {"status": "applied" if status == "OK" else "ambiguous"}

    def dispatch_archive(self, mutation):
        return self._move(mutation, mutation["before"], self.archive)

    def restore_archive(self, mutation, expected):
        return self._move(mutation, expected, "INBOX")
