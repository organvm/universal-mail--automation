"""Minimal native archive dispatch with truthful concurrency guarantees.

Gmail modify and Graph move have no atomic revision precondition here. The engine
owns local locks, fresh observations, persisted intent and server reconciliation.
Ambiguous dispatch never retries. Guard callbacks re-read protection/overrides.
"""
import base64
import hashlib
from urllib.parse import quote

from core.flag_workflow import sha256_hex
from providers.outlook import GRAPH_API_BASE


CONTRACT = {"schema": "uma.archive_adapter.v2", "local_writer_lock": True,
            "local_writer_lock_scope": "archive_state_directory",
            "server_revision_precondition": "none", "dispatch": "single_attempt",
            "verification": "positive_preservation_and_inbox_absence"}


class GmailArchive:
    archive_contract = {**CONTRACT, "provider": "gmail", "mutation": "remove_INBOX_only"}

    def __init__(self, service, *, account, guard):
        self.service, self.account, self.guard = service, account, guard
        profile = service.users().getProfile(userId="me").execute()
        if profile["emailAddress"].casefold() != account.casefold():
            raise ValueError("Gmail authenticated identity mismatch")

    def _id(self, mutation):
        identity = mutation["identity"]
        if identity["provider"] != "gmail" or identity["account"] != self.account:
            raise ValueError("Gmail archive account mismatch")
        # Inventory from Gmail IMAP exposes the same immutable 64-bit ID in decimal.
        return format(int(identity["message_id"]), "x") if mutation["before"].get("id_format") == "decimal" else identity["message_id"]

    def observe_archive(self, mutation):
        mid = self._id(mutation)
        record = self.service.users().messages().get(userId=self.account, id=mid, format="raw").execute()
        if record["id"] != mid:
            raise ValueError("Gmail message identity mismatch")
        raw = base64.urlsafe_b64decode(record["raw"] + "=" * (-len(record["raw"]) % 4))
        header, separator, _ = raw.partition(b"\r\n\r\n")
        if not separator or sha256_hex({"headers": (header + separator).decode("utf-8", errors="replace")}) != mutation["identity"]["evidence_digest"]:
            raise ValueError("Gmail header evidence changed")
        labels = sorted(record["labelIds"])
        guard = self.guard(mutation["identity"])
        return {"identity": mutation["identity"], "server_confirmed": True, "message_present": True,
                "in_inbox": "INBOX" in labels, "label_ids": labels, "revision": str(record["historyId"]),
                "content_sha256": hashlib.sha256(raw).hexdigest(),
                "preserved_sha256": sha256_hex({"content": hashlib.sha256(raw).hexdigest(),
                                                 "labels": [label for label in labels if label != "INBOX"]}),
                "protected": guard["protected"], "human_override": guard["human_override"],
                "archive_destination": "All Mail", "id_format": mutation["before"].get("id_format", "hex")}

    def dispatch_archive(self, mutation):
        if self.observe_archive(mutation) != mutation["before"]:
            return {"status": "not_dispatched", "reason": "fresh_observation_changed"}
        try:
            self.service.users().messages().modify(userId=self.account, id=self._id(mutation),
                body={"removeLabelIds": ["INBOX"]}).execute(num_retries=0)
        except Exception:
            return {"status": "ambiguous", "reason": "dispatch_outcome_unknown"}
        return {"status": "applied"}

    def restore_archive(self, mutation, expected):
        if self.observe_archive(mutation) != expected or expected["protected"] or expected["human_override"]:
            return {"status": "not_dispatched", "reason": "human_or_server_state_changed"}
        try:
            self.service.users().messages().modify(userId=self.account, id=self._id(mutation),
                body={"addLabelIds": ["INBOX"]}).execute(num_retries=0)
        except Exception:
            return {"status": "ambiguous"}
        return {"status": "applied"}


class GraphArchive:
    archive_contract = {**CONTRACT, "provider": "outlook", "mutation": "move_to_verified_archive"}

    def __init__(self, provider, *, guard):
        if not provider.account or not provider._access_token:
            raise ValueError("authenticated explicit Outlook account required")
        self.provider, self.guard = provider, guard
        self.inbox = provider._api_get(GRAPH_API_BASE + "/me/mailFolders/inbox")["id"]
        self.archive = provider._api_get(GRAPH_API_BASE + "/me/mailFolders/archive")["id"]
        if self.inbox == self.archive:
            raise ValueError("Archive cannot be Inbox")

    def _url(self, mutation):
        identity = mutation["identity"]
        if identity["provider"] != "outlook" or identity["account"] != self.provider.account:
            raise ValueError("Outlook archive account mismatch")
        return GRAPH_API_BASE + "/me/messages/" + quote(identity["message_id"], safe="")

    def observe_archive(self, mutation):
        url = self._url(mutation)
        row = self.provider._api_get(url, params={"$select": "id,parentFolderId,changeKey,isRead,flag,categories,importance,internetMessageId,internetMessageHeaders"})
        if row["id"] != mutation["identity"]["message_id"]:
            raise ValueError("Outlook immutable identity changed")
        from providers.graph_inventory import read_mime, mime_headers
        raw = read_mime(self.provider, mutation["identity"]["message_id"])
        header_source = mutation["before"].get("header_source", "graph")
        headers = mime_headers(raw) if header_source == "mime" else row.get("internetMessageHeaders")
        if headers is None or sha256_hex(headers) != mutation["identity"]["evidence_digest"]:
            raise ValueError("Outlook header evidence changed")
        content = hashlib.sha256(raw).hexdigest()
        guard = self.guard(mutation["identity"])
        return {"identity": mutation["identity"], "server_confirmed": True, "message_present": True,
                "in_inbox": row["parentFolderId"] == self.inbox, "mailboxes": [row["parentFolderId"]],
                "revision": row["changeKey"], "content_sha256": content, "header_source": header_source,
                "preserved_sha256": sha256_hex({"content": content, "isRead": row["isRead"],
                    "flag": row["flag"], "categories": sorted(row["categories"]), "importance": row["importance"]}),
                "archive_destination": self.archive, "protected": guard["protected"],
                "human_override": guard["human_override"]}

    def dispatch_archive(self, mutation):
        if mutation["destination"] != self.archive or self.observe_archive(mutation) != mutation["before"]:
            return {"status": "not_dispatched", "reason": "fresh_observation_changed"}
        try:
            result = self.provider._api_post(self._url(mutation) + "/move", {"destinationId": self.archive})
        except Exception:
            return {"status": "ambiguous"}
        if result.get("id") != mutation["identity"]["message_id"]:
            return {"status": "ambiguous", "reason": "immutable_id_not_preserved"}
        return {"status": "applied", "destination_id": self.archive}

    def restore_archive(self, mutation, expected):
        if self.observe_archive(mutation) != expected or expected["protected"] or expected["human_override"]:
            return {"status": "not_dispatched", "reason": "human_or_server_state_changed"}
        try:
            self.provider._api_post(self._url(mutation) + "/move", {"destinationId": self.inbox})
        except Exception:
            return {"status": "ambiguous"}
        return {"status": "applied"}
