import pytest

from core.corpus_research import research_corpus, extract_evidence
from core.mail_inventory import seal


def corpus(count=45):
    return seal({"schema": "uma.research_corpus.v1", "threads": [{"id": "thread", "messages": [
        {"id": str(i), "provenance": [{"identity": {"account": "a"}}]} for i in range(count)]}]})


class Provider:
    def __init__(self):
        self.reads = []
        self.fail = None

    def read_corpus_message(self, item):
        if item["id"] == self.fail:
            raise RuntimeError("offline")
        self.reads.append(item["id"])
        return {"headers": [], "complete": True}


def test_long_thread_resumes_between_reads(tmp_path):
    source, provider = corpus(), Provider()
    assert not research_corpus(source, provider, root=tmp_path)["complete"]
    assert len(provider.reads) == 20
    assert not research_corpus(source, provider, root=tmp_path)["complete"]
    assert len(provider.reads) == 40
    assert research_corpus(source, provider, root=tmp_path)["complete"]
    assert provider.reads == [str(i) for i in range(45)]


def test_failed_read_keeps_exact_checkpoint_and_prior_evidence(tmp_path):
    source, provider = corpus(5), Provider()
    provider.fail = "2"
    first = research_corpus(source, provider, root=tmp_path)
    assert first["threads"][0]["next_message"] == 2
    provider.fail = None
    assert research_corpus(source, provider, root=tmp_path)["complete"]
    assert provider.reads == [str(i) for i in range(5)]


def test_changed_corpus_cannot_reuse_progress(tmp_path):
    research_corpus(corpus(), Provider(), root=tmp_path)
    with pytest.raises(ValueError, match="lineage"):
        research_corpus(corpus(46), Provider(), root=tmp_path)


def test_attachment_provenance_remains_unresolved():
    raw = b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nPlease review.\r\n--b\r\nContent-Type: text/plain\r\nContent-Disposition: attachment; filename="note.txt"\r\n\r\nEvidence\r\n--b--\r\n'
    result = extract_evidence(raw)
    assert result["attachments"][0]["filename"] == "note.txt"
    assert result["attachments"][0]["review_status"] == "requires_relevance_review"
    assert result["bodies"][0]["text"] == "Please review."
    assert seal(result)["content_hash"]
