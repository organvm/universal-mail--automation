"""Account-bound IMAP inventory with UIDVALIDITY snapshots and BODY.PEEK reads."""
from __future__ import annotations

import base64
import email
from email import policy
import imaplib
import re
import shlex

from core.flag_workflow import sha256_hex


def decode_mailbox(value: str) -> str:
    def decode(match):
        part = match.group(1)
        if not part:
            return "&"
        return base64.b64decode(part.replace(",", "/") + "=" * (-len(part) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", decode, value)


def quote(value: str) -> str:
    if any(c in value for c in "\r\n\x00"):
        raise ValueError("invalid IMAP mailbox")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class IMAPInventory:
    """Connection must already have authenticated as the explicit account.

    The factory owns login; callers cannot use this adapter as evidence of another
    account. Gmail retains both global X-GM-MSGID and each folder's UID identity.
    """
    def __init__(self, connection, *, account: str, provider: str, host: str):
        if not account or provider not in ("gmail", "icloud", "outlook", "imap"):
            raise ValueError("explicit provider/account required")
        if connection.state != "AUTH":
            raise ValueError("authenticated IMAP connection required")
        self.connection = connection
        self.account = account
        self.provider = provider
        self.host = host
        self.gmail = "X-GM-EXT-1" in {
            x.decode() if isinstance(x, bytes) else x for x in connection.capabilities}
        if provider == "gmail" and not self.gmail:
            raise ValueError("Gmail native identity extension required")

    def identity(self):
        return {"provider": self.provider, "account": self.account, "host": self.host,
                "transport": "imap", "authenticated": True}

    def inventory_surfaces(self):
        status, data = self.connection.list()
        if status != "OK" or data is None:
            raise RuntimeError("IMAP LIST failed")
        result = []
        for raw in data:
            if not isinstance(raw, bytes):
                raise ValueError("unsupported IMAP LIST literal")
            match = re.fullmatch(rb'\((.*?)\)\s+("(?:[^"\\]|\\.)*"|NIL)\s+(.+)', raw)
            if not match:
                raise ValueError("malformed IMAP LIST")
            attributes = match[1].decode().split()
            tail = match[3].decode("ascii")
            wire = shlex.split(tail)[0] if tail.startswith('"') else tail
            name = decode_mailbox(wire)
            lowered = {a.casefold() for a in attributes}
            excluded = bool(lowered & {"\\trash", "\\junk"}) or name.casefold() in {"trash", "junk", "junk email", "deleted messages", "deleted items", "[gmail]/trash", "[gmail]/spam"}
            result.append({"id": wire, "name": name, "attributes": attributes,
                           "selectable": "\\noselect" not in lowered,
                           "retention_class": "junk_trash" if excluded else "retained"})
        return sorted(result, key=lambda s: (s["retention_class"] != "retained", s["id"]))

    def _select(self, surface):
        status, counts = self.connection.select(quote(surface["id"]), readonly=True)
        if status != "OK":
            raise RuntimeError("IMAP EXAMINE failed")
        self.selected_count = int(counts[0]) if counts and counts[0] is not None else None
        _, validity = self.connection.response("UIDVALIDITY")
        if not validity or validity[0] is None:
            raise RuntimeError("UIDVALIDITY missing")
        return validity[0].decode() if isinstance(validity[0], bytes) else str(validity[0])

    def inventory_snapshot(self, surface):
        validity = self._select(surface)
        status, data = self.connection.uid("SEARCH", None, "ALL")
        # iCloud omits an untagged SEARCH payload for an empty selected folder.
        # Accept that only with independent EXAMINE count=0 and tagged OK.
        if status == "OK" and data == [None] and self.selected_count == 0:
            data = [b""]
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise RuntimeError("IMAP UID SEARCH failed")
        uids = data[0].decode().split()
        if any(not u.isdigit() or int(u) <= 0 for u in uids) or len(set(uids)) != len(uids):
            raise ValueError("invalid UID snapshot")
        return {"uidvalidity": validity, "uids": sorted(uids, key=int),
                "boundary": "retained_uid_set_at_scan_start"}

    def inventory_page(self, surface, snapshot, cursor, limit):
        if self._select(surface) != snapshot["uidvalidity"]:
            raise RuntimeError("UIDVALIDITY changed; rescan requires a new inventory")
        offset = 0 if cursor is None else cursor
        if type(offset) is not int or not 0 <= offset <= len(snapshot["uids"]):
            raise ValueError("invalid IMAP continuation")
        selected = snapshot["uids"][offset:offset + limit]
        messages = []
        if selected:
            extra = " X-GM-MSGID X-GM-THRID X-GM-LABELS" if self.gmail else ""
            status, data = self.connection.uid("FETCH", ",".join(selected),
                "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER]" + extra + ")")
            if status != "OK" or data is None:
                raise RuntimeError("IMAP FETCH failed")
            for item in data:
                if not isinstance(item, tuple):
                    continue
                meta, headers = item
                uid_match = re.search(rb"\bUID (\d+)\b", meta)
                if uid_match is None or not isinstance(headers, bytes):
                    raise ValueError("incomplete IMAP header record")
                uid = uid_match[1].decode()
                msgid = re.search(rb"X-GM-MSGID (\d+)", meta)
                thread = re.search(rb"X-GM-THRID (\d+)", meta)
                if self.gmail and (msgid is None or thread is None):
                    raise ValueError("Gmail global identity missing")
                parsed = email.message_from_bytes(headers, policy=policy.default)
                native = {"mailbox": surface["id"], "uidvalidity": snapshot["uidvalidity"], "uid": uid}
                messages.append({"identity": {"provider": self.provider, "account": self.account,
                    "message_id": msgid[1].decode() if msgid else sha256_hex(native),
                    "evidence_digest": sha256_hex({"headers": headers.decode("utf-8", errors="replace")})},
                    "native": native, "memberships": [surface["id"]],
                    "retention_class": surface["retention_class"],
                    "thread_id": thread[1].decode() if thread else str(parsed.get("Message-ID", "")),
                    "rfc_message_id": str(parsed.get("Message-ID", "")),
                    "in_reply_to": str(parsed.get("In-Reply-To", "")),
                    "references": str(parsed.get("References", "")),
                    "sender": str(parsed.get("From", "")), "subject": str(parsed.get("Subject", "")),
                    "date": str(parsed.get("Date", "")),
                    "headers": headers.decode("utf-8", errors="replace"),
                    "native_flags": [f.decode() for f in imaplib.ParseFlags(meta)],
                    "server_metadata": meta.decode("utf-8", errors="replace")})
            returned = [m["native"]["uid"] for m in messages]
            if set(returned) != set(selected) or len(returned) != len(selected):
                raise RuntimeError("snapshot messages missing or duplicated; page remains incomplete")
        next_offset = offset + len(selected)
        complete = next_offset == len(snapshot["uids"])
        return {"messages": messages, "complete": complete,
                "next_cursor": None if complete else next_offset}

    def read_corpus_message(self, item):
        from core.corpus_research import extract_evidence
        provenance = item["provenance"][0]
        identity, native = provenance["identity"], provenance["native"]
        if identity["account"] != self.account or identity["provider"] != self.provider:
            raise ValueError("research account mismatch")
        if self._select({"id": native["mailbox"]}) != native["uidvalidity"]:
            raise RuntimeError("research UIDVALIDITY changed")
        extra = " X-GM-MSGID" if self.gmail else ""
        header_request = " BODY.PEEK[HEADER]" if self.gmail else ""
        status, data = self.connection.uid("FETCH", native["uid"],
            "(UID RFC822.SIZE BODY.PEEK[]<0.10485760>" + header_request + extra + ")")
        literals = [d for d in data or [] if isinstance(d, tuple)]
        if status != "OK" or len(literals) != (2 if self.gmail else 1):
            raise RuntimeError("research exact message unavailable")
        metadata = b" ".join(d[0] if isinstance(d, tuple) else d for d in data if isinstance(d, (tuple, bytes)))
        bodies = [payload for meta, payload in literals if b"BODY[]" in meta]
        if len(bodies) != 1:
            raise RuntimeError("research full-message literal missing or duplicated")
        raw = bodies[0]
        uids = re.findall(rb"\bUID (\d+)\b", metadata)
        size = re.search(rb"RFC822.SIZE (\d+)", metadata)
        if uids != [native["uid"].encode()] or size is None or len(raw) != int(size[1]):
            raise RuntimeError("research message incomplete or oversized")
        if self.gmail:
            mids = re.findall(rb"X-GM-MSGID (\d+)", metadata)
            if mids != [identity["message_id"].encode()]:
                raise ValueError("Gmail research identity changed")
        header, sep, _ = raw.partition(b"\r\n\r\n")
        if not sep:
            raise ValueError("RFC header boundary missing")
        # Gmail folds BODY[HEADER] differently from BODY[]. Bind the exact
        # inventory representation and the full MIME to the SAME UID/X-GM-MSGID
        # FETCH response; never normalize away changed header content.
        headers = [payload for meta, payload in literals if b"BODY[HEADER]" in meta] if self.gmail else [header + sep]
        if len(headers) != 1:
            raise RuntimeError("research header literal missing or duplicated")
        digest = sha256_hex({"headers": headers[0].decode("utf-8", errors="replace")})
        if digest != identity["evidence_digest"]:
            raise ValueError("research header evidence changed")
        return {**extract_evidence(raw), "raw_rfc822_base64": base64.b64encode(raw).decode(),
                "native_headers_sha256": digest,
                "header_representation": "imap_header_same_fetch" if self.gmail else "raw_mime",
                "raw_sha256": __import__("hashlib").sha256(raw).hexdigest(), "complete": True}
