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


def test_valid_receipts_cannot_be_swapped_between_surfaces(tmp_path):
    from core.mail_inventory import seal
    provider = Provider()
    result = inventory(provider, root=tmp_path)
    left, right = result["surfaces"]
    left["pages"], right["pages"] = right["pages"], left["pages"]
    (tmp_path / "manifest.json").write_text(json.dumps(seal(result)))
    with pytest.raises(ValueError, match="lineage"):
        list(iter_messages(tmp_path))


def test_complete_receipt_cannot_omit_a_native_snapshot_uid(tmp_path):
    from core.mail_inventory import seal, validated_pages
    from core.flag_workflow import sha256_hex
    snapshot = {"uids": ["1", "2"], "uidvalidity": "123"}
    surface, identity = {"id": "Inbox"}, {"account": "a"}
    page = seal({"schema": "uma.mail_inventory_page.v1", "identity": identity,
                 "surface": surface, "snapshot_sha256": sha256_hex(snapshot),
                 "cursor": None, "complete": True, "next_cursor": None,
                 "messages": [{"native": {"uid": "1", "uidvalidity": "123", "mailbox": "Inbox"}}]})
    (tmp_path / "pages").mkdir()
    name = "pages/" + page["content_hash"] + ".json"
    (tmp_path / name).write_text(json.dumps(page))
    state = {"surface": surface, "snapshot": snapshot, "cursor": None, "status": "complete",
             "pages": [{"file": name, "sha256": page["content_hash"], "messages": 1}]}
    with pytest.raises(ValueError, match="omits"):
        list(validated_pages(tmp_path, {"identity": identity}, state))


@pytest.mark.parametrize("reversed_literals", [False, True])
@pytest.mark.parametrize("size_delta,tail_payload", [(0, None), (-3, b""), (-3, b"x"), (3, b"")])
def test_gmail_full_read_binds_differently_folded_headers_in_same_fetch(reversed_literals, size_delta, tail_payload):
    from core.flag_workflow import sha256_hex
    headers = b"Subject: a long subject\r\nMessage-ID: <one@example.invalid>\r\n\r\n"
    raw = b"Subject: a long\r\n subject\r\nMessage-ID: <one@example.invalid>\r\n\r\nContent"

    class Connection:
        state, capabilities = "AUTH", [b"X-GM-EXT-1"]

        def select(self, mailbox, readonly):
            assert readonly
            return "OK", [b"1"]

        def response(self, name):
            return name, [b"123"]

        def uid(self, command, uid, query):
            if "BODY.PEEK[HEADER]" not in query:
                assert query == f"(UID X-GM-MSGID BODY.PEEK[]<{len(raw)}.1>)"
                return "OK", [(f"1 (UID 1 X-GM-MSGID 42 BODY[]<{len(raw)}>".encode(), tail_payload), b")"]
            assert "BODY.PEEK[HEADER]" in query and "BODY.PEEK[]" in query
            parts = [(b"BODY[HEADER]", headers), (b"BODY[]<0>", raw)]
            if reversed_literals:
                parts.reverse()
            meta, payload = parts[0]
            parts[0] = (b"1 (UID 1 RFC822.SIZE " + str(len(raw) + size_delta).encode() + b" " + meta, payload)
            return "OK", parts + [b" X-GM-MSGID 42)"]

    identity = {"account": "a", "provider": "gmail", "message_id": "42",
                "evidence_digest": sha256_hex({"headers": headers.decode()})}
    item = {"provenance": [{"identity": identity, "native": {"mailbox": "Inbox", "uid": "1", "uidvalidity": "123"}}]}
    adapter = IMAPInventory(Connection(), account="a", provider="gmail", host="imap.gmail.com")
    if tail_payload == b"x":
        with pytest.raises(RuntimeError, match="incomplete"):
            adapter.read_corpus_message(item)
        return
    result = adapter.read_corpus_message(item)
    assert result["complete"] and result["native_headers_sha256"] == identity["evidence_digest"]
    assert result["header_representation"] == "imap_header_same_fetch"
    identity["evidence_digest"] = "0" * 64
    with pytest.raises(ValueError, match="header evidence changed"):
        adapter.read_corpus_message(item)
