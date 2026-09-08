import json

import pytest

from core.github_evidence import discover_signals, resolve_batch, resolve_signal


def signal():
    return discover_signals([{"identity": {"account": "a", "message_id": "1"},
                              "headers": "<owner/repo/pull/42@github.com>"}])[0]


class Reader:
    def __init__(self, merged=True):
        self.calls = []
        self.merged = merged

    def get(self, path, *, paginate=False):
        self.calls.append((path, paginate))
        if path == "user":
            return {"login": "owner"}
        if path == "repos/owner/repo/pulls/42":
            return {"number": 42, "state": "closed", "merged": self.merged}
        if paginate:
            return [[{"id": 1}], [{"id": 2}]]
        raise AssertionError(path)


def test_rfc_and_url_duplicates_retain_each_notification():
    messages = [{"message_id": "a", "headers": "<owner/repo/pull/42@github.com>"},
                {"message_id": "b", "bodies": [{"text": "https://github.com/owner/repo/pull/42#discussion_r1"}]}]
    result = discover_signals(messages)
    assert len(result) == 1
    assert {n["message"] for n in result[0]["notifications"]} == {"a", "b"}


def test_resolution_uses_exact_object_and_all_review_pages():
    reader = Reader()
    result = resolve_signal(signal(), reader)
    assert result["status"] == "resolved"
    assert reader.calls == [("repos/owner/repo/pulls/42", False),
        ("repos/owner/repo/pulls/42/reviews?per_page=100", True),
        ("repos/owner/repo/issues/42/comments?per_page=100", True)]
    assert len(result["receipts"][1]["evidence"]) == 2
    assert resolve_signal(signal(), Reader(False))["status"] == "unresolved"


def test_failed_page_does_not_advance_resume(tmp_path):
    reader = Reader()
    original = reader.get
    def fail(path, **kwargs):
        if "reviews" in path:
            raise RuntimeError("failed second page")
        return original(path, **kwargs)
    reader.get = fail
    result = resolve_batch([signal()], root=tmp_path, reader=reader)
    assert result["next_object"] == 0
    assert result["results"] == []
    reader.get = original
    assert resolve_batch([signal()], root=tmp_path, reader=reader)["next_object"] == 1


def test_tampered_completed_receipt_cannot_be_skipped(tmp_path):
    result = resolve_batch([signal()], root=tmp_path, reader=Reader())
    path = tmp_path / result["results"][0]["file"]
    evidence = json.loads(path.read_text())
    evidence["status"] = "unresolved"
    path.write_text(json.dumps(evidence))
    with pytest.raises(ValueError):
        resolve_batch([signal()], root=tmp_path, reader=Reader())
