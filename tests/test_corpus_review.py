from datetime import datetime, timezone

import pytest

from core.corpus_research import research_corpus
from core.corpus_review import record_review, archive_coverage, readable_body
from core.flag_workflow import _json_object_from_path
from core.mail_inventory import seal
from core.obligation_workflow import Obligation, Evidence, MessageIdentity


def fixture(tmp_path):
    identity = MessageIdentity(provider="gmail", account="a@example.invalid", message_id="123", evidence_digest="a" * 64)
    provenance = [{"identity": identity.model_dump(), "native": {"uid": "1"}, "memberships": ["INBOX"]}]
    corpus = seal({"schema": "uma.research_corpus.v1", "sources": [{"inventory_sha256": "b" * 64}],
                   "threads": [{"id": "thread", "messages": [{"id": "message", "provenance": provenance}]}]})
    class Provider:
        def read_corpus_message(self, message):
            return {"headers": [], "bodies": [], "attachments": [], "complete": True}
    research = tmp_path / "research"
    state = research_corpus(corpus, Provider(), root=research)
    receipt = state["threads"][0]["receipts"][0]["sha256"]
    now = datetime.now(timezone.utc)
    obligation = Obligation(id="notice", title="Reviewed non-action notice", thread_id="thread", messages=(identity,),
        evidence=(Evidence(id="notice-evidence", obligation_id="notice", message=identity,
            occurred_at=now, observed_at=now, source="correspondence", fact="non_action", next_actor="none",
            proof="sha256:" + receipt),))
    review = {"schema": "uma.correspondence_review.v1", "corpus_sha256": corpus["content_hash"], "thread_id": "thread",
              "message_receipts": [receipt], "obligations": [obligation.model_dump(mode="json")], "attachment_reviews": []}
    return corpus, research, review, identity


def test_review_and_coverage_bind_complete_obligation_set(tmp_path):
    corpus, research, review, identity = fixture(tmp_path)
    root = tmp_path / "reviews"
    result = record_review(corpus, research_root=research, root=root, review=review)
    assert result["authority"] == "review_only"
    coverage = archive_coverage(corpus, research_root=research, reviews_root=root, message_keys=[identity.key])
    assert coverage["schema"] == "uma.archive_coverage.v2"
    assert set(coverage["reviewed_obligations"]) == {"notice"}
    assert coverage["message_keys"] == [identity.key]


def test_omitted_receipt_or_unbound_proof_cannot_be_reviewed(tmp_path):
    corpus, research, review, _ = fixture(tmp_path)
    review["message_receipts"] = []
    with pytest.raises(ValueError, match="all messages"):
        record_review(corpus, research_root=research, root=tmp_path / "reviews", review=review)
    state = _json_object_from_path(research / "manifest.json", "research")
    review["message_receipts"] = [state["threads"][0]["receipts"][0]["sha256"]]
    review["obligations"][0]["evidence"][0]["proof"] = "an unrelated receipt"
    with pytest.raises(ValueError, match="proof"):
        record_review(corpus, research_root=research, root=tmp_path / "reviews", review=review)


def test_review_changes_require_explicit_supersession(tmp_path):
    corpus, research, review, _ = fixture(tmp_path)
    root = tmp_path / "reviews"
    first = record_review(corpus, research_root=research, root=root, review=review)
    with pytest.raises(ValueError, match="supersede"):
        record_review(corpus, research_root=research, root=root, review=review)
    review["supersedes"] = first["content_hash"]
    second = record_review(corpus, research_root=research, root=root, review=review)
    assert second["content_hash"] != first["content_hash"]
    assert (root / "history" / (first["content_hash"] + ".json")).exists()


def test_readable_body_omits_css_but_keeps_obligation_text():
    result = readable_body({"bodies": [{"content_type": "text/html", "text":
        "<style>red {color:red}</style><p>Please review the attachment.</p><p>Due tomorrow.</p>"}]})
    assert result == "Please review the attachment.\nDue tomorrow."
