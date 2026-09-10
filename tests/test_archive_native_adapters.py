import base64
from copy import deepcopy
import hashlib

import pytest

from core.archive_transactions import verify_observation
from core.flag_workflow import sha256_hex
from providers.archive_native import GmailArchive, GraphArchive
from providers.imap_archive_native import IMAPArchive


RAW = b"Message-ID: <fixture@example.invalid>\r\nSubject: Fixture\r\n\r\nRetained evidence."
HEADERS = [{"name": "Message-ID", "value": "<fixture@example.invalid>"}]


def guard(identity):
    return {"protected": False, "human_override": False}


class Response:
    def __init__(self, value):
        self.value = value

    def execute(self, **kwargs):
        return deepcopy(self.value)


class Gmail:
    def __init__(self):
        self.record = {"id": "7b", "historyId": "1", "labelIds": ["INBOX", "STARRED", "Label_1"],
                       "raw": base64.urlsafe_b64encode(RAW).decode()}
        self.writes = []

    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, **kwargs):
        return Response({"emailAddress": "account@example.invalid"})

    def get(self, **kwargs):
        assert kwargs["id"] == "7b"
        return Response(self.record)

    def modify(self, **kwargs):
        self.writes.append(kwargs)
        self.record["labelIds"].remove("INBOX")
        self.record["historyId"] = "2"
        return Response(self.record)


def gmail_mutation():
    header, separator, _ = RAW.partition(b"\r\n\r\n")
    return {"identity": {"provider": "gmail", "account": "account@example.invalid", "message_id": "123",
                         "evidence_digest": sha256_hex({"headers": (header + separator).decode()})},
            "before": {"id_format": "decimal"}, "destination": "All Mail"}


def test_gmail_dispatch_removes_only_inbox_and_proves_preservation():
    service = Gmail()
    adapter = GmailArchive(service, account="account@example.invalid", guard=guard)
    mutation = gmail_mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "applied"
    assert service.writes[0]["body"] == {"removeLabelIds": ["INBOX"]}
    observed = adapter.observe_archive(mutation)
    assert verify_observation(mutation, observed, native_v2=True) == "verified"
    assert observed["label_ids"] == ["Label_1", "STARRED"]
    service.record["labelIds"].append("TRASH")
    assert verify_observation(mutation, adapter.observe_archive(mutation), native_v2=True) == "conflicted"


def test_gmail_wrong_account_and_changed_content_never_dispatch():
    service = Gmail()
    with pytest.raises(ValueError, match="identity"):
        GmailArchive(service, account="other@example.invalid", guard=guard)
    adapter = GmailArchive(service, account="account@example.invalid", guard=guard)
    mutation = gmail_mutation()
    mutation["before"] = adapter.observe_archive(mutation)
    service.record["raw"] = base64.urlsafe_b64encode(RAW + b" changed").decode()
    assert adapter.dispatch_archive(mutation)["status"] == "not_dispatched"
    assert not service.writes


class MIME:
    def raise_for_status(self):
        pass

    def iter_content(self, size):
        yield RAW

    def close(self):
        pass


class Graph:
    def __init__(self, account):
        self.account = account
        self._access_token = "fixture"
        self.row = {"id": "immutable/id", "parentFolderId": "inbox", "changeKey": "1", "isRead": False,
                    "flag": {"flagStatus": "flagged"}, "categories": ["work"], "importance": "normal",
                    "internetMessageHeaders": HEADERS}
        self.writes = []

    def _api_get(self, url, **kwargs):
        if "/mailFolders/" in url:
            return {"id": url.rsplit("/", 1)[-1]}
        assert url.endswith("/messages/immutable%2Fid")
        return deepcopy(self.row)

    def _get_session(self):
        return self

    def get(self, url, **kwargs):
        assert url.endswith("/messages/immutable%2Fid/$value")
        return MIME()

    def _api_post(self, url, body):
        self.writes.append((url, body))
        self.row.update(parentFolderId=body["destinationId"], changeKey="2")
        return deepcopy(self.row)


@pytest.mark.parametrize("account", ["first@example.invalid", "second@example.invalid"])
def test_graph_moves_exact_immutable_identity_and_preserves_unread_flags(account):
    provider = Graph(account)
    adapter = GraphArchive(provider, guard=guard)
    mutation = {"identity": {"provider": "outlook", "account": account, "message_id": "immutable/id",
                             "evidence_digest": sha256_hex(HEADERS)}, "before": {}, "destination": "archive"}
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "applied"
    assert provider.writes[0][1] == {"destinationId": "archive"}
    observed = adapter.observe_archive(mutation)
    assert verify_observation(mutation, observed, native_v2=True) == "verified"
    assert observed["content_sha256"] == hashlib.sha256(RAW).hexdigest()
    provider.row["isRead"] = True
    assert verify_observation(mutation, adapter.observe_archive(mutation), native_v2=True) == "conflicted"


class IMAP:
    capabilities = (b"IMAP4rev1", b"MOVE")

    def __init__(self, retain_source):
        self.folders = {"INBOX": {"1": RAW}, "Archive": {}}
        self.retain_source = retain_source
        self.commands = []

    def select(self, mailbox, readonly):
        self.selected = mailbox.strip('"')
        return "OK", [str(len(self.folders[self.selected])).encode()]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return "UIDVALIDITY", [b"7"]

    def uid(self, command, *args):
        self.commands.append(command)
        folder = self.folders[self.selected]
        if command == "SEARCH":
            return "OK", [" ".join(folder).encode()]
        if command == "FETCH":
            raw = folder[args[0]]
            metadata = f"1 (UID {args[0]} FLAGS (\\Flagged) RFC822.SIZE {len(raw)}".encode()
            return "OK", [(metadata, raw), b")"]
        assert command == "MOVE"
        target = args[1].strip('"')
        self.folders[target]["2"] = folder[args[0]]
        if not self.retain_source:
            del folder[args[0]]
        return "OK", [b"moved"]


@pytest.mark.parametrize("retain_source", [False, True])
def test_imap_requires_preserved_destination_and_source_absence(retain_source):
    connection = IMAP(retain_source)
    native = {"mailbox": "INBOX", "uidvalidity": "7", "uid": "1"}
    mutation = {"identity": {"provider": "icloud", "account": "account@example.invalid",
                             "message_id": sha256_hex(native), "evidence_digest": "a" * 64},
                "before": {"rfc_message_id": "<fixture@example.invalid>",
                           "content_sha256": hashlib.sha256(RAW).hexdigest(), "original_native": native},
                "destination": "Archive"}
    adapter = IMAPArchive(connection, account="account@example.invalid", archive_mailbox="Archive", guard=guard)
    mutation["before"] = adapter.observe_archive(mutation)
    assert adapter.dispatch_archive(mutation)["status"] == "applied"
    result = verify_observation(mutation, adapter.observe_archive(mutation), native_v2=True)
    assert result == ("uncertain" if retain_source else "verified")
    assert "EXPUNGE" not in connection.commands
