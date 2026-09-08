import json

import pytest

from core.mail_inventory import inventory, iter_messages
from providers.imap_inventory import IMAPInventory, decode_mailbox


class Provider:
    def __init__(self):
        self.reads = []
        self.fail = False

    def identity(self):
        return {"account": "a@example.invalid", "authenticated": True}

    def inventory_surfaces(self):
        return [{"id": "Inbox"}, {"id": "Archive"}]

    def inventory_snapshot(self, surface):
        return {"boundary": "frozen"}

    def inventory_page(self, surface, snapshot, cursor, limit):
        self.reads.append((surface["id"], cursor))
        if self.fail:
            raise RuntimeError("offline")
        return {"messages": [{"native": surface["id"] + str(cursor)}],
                "complete": cursor == 1, "next_cursor": 1 if cursor is None else None}


def test_inventory_resumes_exact_pages_and_preserves_all_memberships(tmp_path):
    provider = Provider()
    first = inventory(provider, root=tmp_path, max_pages=1)
    assert not first["complete"]
    second = inventory(provider, root=tmp_path)
    assert second["complete"]
    assert provider.reads == [("Inbox", None), ("Inbox", 1), ("Archive", None), ("Archive", 1)]
    assert len(list(iter_messages(tmp_path))) == 4
    assert (tmp_path / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_failed_page_does_not_advance(tmp_path):
    provider = Provider()
    provider.fail = True
    result = inventory(provider, root=tmp_path)
    assert not result["complete"]
    assert all(s["cursor"] is None and not s["pages"] for s in result["surfaces"])
    provider.fail = False
    assert inventory(provider, root=tmp_path)["complete"]


def test_resume_checks_receipt_integrity_before_read(tmp_path):
    provider = Provider()
    result = inventory(provider, root=tmp_path, max_pages=1)
    page = tmp_path / result["surfaces"][0]["pages"][0]["file"]
    body = json.loads(page.read_text())
    body["messages"] = []
    page.write_text(json.dumps(body))
    with pytest.raises(ValueError, match="hash"):
        inventory(provider, root=tmp_path)
    assert len(provider.reads) == 1


def test_imap_missing_uid_is_not_empty_success():
    class Connection:
        state = "AUTH"
        capabilities = []

        def select(self, mailbox, readonly):
            assert readonly
            return "OK", [b"1"]

        def response(self, name):
            return name, [b"123"]

        def uid(self, *args):
            return "OK", [None]
    provider = IMAPInventory(Connection(), account="a@example.invalid", provider="icloud", host="example.invalid")
    with pytest.raises(RuntimeError, match="missing"):
        provider.inventory_page({"id": "Inbox"}, {"uidvalidity": "123", "uids": ["1"]}, None, 20)


def test_modified_utf7_mailbox_names():
    assert decode_mailbox("Personal &- Work") == "Personal & Work"
    assert decode_mailbox("&ZeVnLIqe-") == "日本語"
