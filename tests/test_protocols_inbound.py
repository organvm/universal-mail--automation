"""Tests for the inbound-lead protocol family — hire / deploy / linkedin classes.

Covers: the CTA-tag match ([… · hire|deploy] — inbound), organic recruiter/client
language, the LinkedIn InMail mirror classifying DESPITE bulk headers + a noreply sender,
job-alert digests NOT classifying, the consequential classes (fraud/security/kyc) still
outranking a spoofed opportunity, Reply-To pass-through, and the first-touch-only, ≤5-per-run
`safe_intent` stamping (never on inbound-linkedin).

Samples are synthetic — they mirror inbound-lead shapes without embedding private mail.
"""

import json

from core.models import EmailMessage
from core.protocols import derive
from providers.base import ListMessagesResult
from inbox_sweep import classify_inbox
from obligations_build import build
from draft_writer import _ob_key


class _FakeProvider:
    """Minimal provider stub for classify_inbox: one page of pre-built EmailMessages."""

    def __init__(self, messages):
        self._messages = messages

    def list_messages(self, query="", limit=50, page_token=None, mailbox=""):
        return ListMessagesResult(messages=self._messages, next_page_token=None)


# --- CTA-tag classification (the positioning surfaces pre-tag the subject) ---

def test_cta_tag_hire_classifies_inbound_lead_hire():
    ob = derive("Jane Recruiter <jane@acme.com>", "[acme-repo · hire] — inbound")
    assert ob.cls == "inbound-lead-hire"
    assert ob.priority == 76
    assert ob.requires_reply is True
    assert ob.verify_first is False
    assert ob.tags == ["opportunity", "career"]
    # MUST NOT carry money/legal/security tags (so it is SAFE-tier eligible, never held).
    assert not ({"money", "legal", "security"} & set(ob.tags))


def test_cta_tag_deploy_classifies_inbound_lead_deploy():
    ob = derive("Bob Client <bob@corp.com>", "[myrepo · deploy] — inbound")
    assert ob.cls == "inbound-lead-deploy"
    assert ob.priority == 76
    assert ob.requires_reply is True
    assert ob.tags == ["opportunity", "client"]
    assert not ({"money", "legal", "security"} & set(ob.tags))


# --- Organic language (a recruiter/client who wrote in their own words) ---

def test_organic_recruiter_language_classifies_hire():
    for subject in (
        "Exciting role at BigCo — talent acquisition",
        "A recruiter reaching out about an opportunity with our team",
        "Sourcing for a senior position at our client",
    ):
        ob = derive("Sam Sender <sam@talent.io>", subject)
        assert ob.cls == "inbound-lead-hire", subject
        assert ob.requires_reply is True, subject


def test_organic_client_language_classifies_deploy():
    for subject in (
        "Interested in a consulting engagement",
        "Would you build this for us — request for a proposal",
        "Quick quote on a deploy",
    ):
        ob = derive("Pat Prospect <pat@startup.com>", subject)
        assert ob.cls == "inbound-lead-deploy", subject
        assert ob.tags == ["opportunity", "client"], subject


# --- LinkedIn InMail mirror: classifies DESPITE bulk headers + a noreply sender ---

def test_linkedin_inmail_classifies_despite_bulk_headers_and_noreply():
    # The whole point: a real inbound arriving through LinkedIn's noreply relay, carrying a
    # List-Unsubscribe header, must STILL classify (a protocol match outranks the bulk gate).
    ob = derive(
        "LinkedIn <noreply@linkedin.com>",
        "You have a new InMail message",
        headers="List-Unsubscribe: <https://www.linkedin.com/unsub>",
    )
    assert ob.cls == "inbound-linkedin"
    assert ob.priority == 70
    assert ob.requires_reply is True
    assert ob.tags == ["opportunity"]


def test_linkedin_connect_and_profile_view_classify():
    for subject in (
        "Alice wants to connect with you",
        "Someone viewed your profile",
        "Bob sent you a message on LinkedIn",
    ):
        ob = derive("LinkedIn <noreply@linkedin.com>", subject,
                    headers="List-Unsubscribe: <x>")
        assert ob.cls == "inbound-linkedin", subject


def test_linkedin_job_alert_digest_does_not_classify_inbound():
    # Job-alert / jobs-you-may / network-hiring digests are NOT a person writing you — they
    # must fall through the inbound-linkedin exclusion to the ordinary bulk gate.
    for subject in (
        "Jobs you may be interested in",
        "Job alert: 12 new roles",
        "See who's hiring in your network this week",
    ):
        ob = derive("LinkedIn Job Alerts <jobs-noreply@linkedin.com>", subject,
                    headers="List-Unsubscribe: <x>")
        assert ob.cls != "inbound-linkedin", subject
        assert ob.cls == "bulk", subject


# --- Consequential classes still outrank a spoofed opportunity (first-match-wins) ---

def test_fraud_outranks_spoofed_opportunity():
    # A "fraud"/"unauthorized" signal wins even when the subject also names an opportunity —
    # the spoofed-opportunity guard: a consequential class is earlier in the list, so it fires
    # first (first-match-wins) and the message is never miscast as a warm lead.
    ob = derive("LinkedIn <noreply@linkedin.com>",
                "Unauthorized charge flagged — also mentions an opportunity with us",
                headers="List-Unsubscribe: <x>")
    assert ob.cls == "fraud-alert"
    assert not ob.cls.startswith("inbound-")


def test_security_outranks_spoofed_opportunity():
    ob = derive("recruiter <r@x.com>", "password recovery code — role at us")
    assert ob.cls == "security-credential-change"


def test_kyc_outranks_spoofed_opportunity():
    ob = derive("recruiter <r@x.com>", "kyc onboarding needed — opportunity at us")
    assert ob.cls == "kyc"


def test_real_person_without_opportunity_still_precedent():
    # Guard: the inbound classes must not swallow an ordinary personal correspondent.
    ob = derive("Micah Longo <micahlongo@gmail.com>", "Depositions next week")
    assert ob.cls == "precedent"
    assert ob.rung == "precedent"


# --- Reply-To pass-through (builder → obligation) ---

def test_reply_to_passes_through_to_obligation(tmp_path):
    receipt = {
        "result": {"account": "acct", "total": 1},
        "rows": [
            {
                "id": "h1", "action": "fire",
                "sender": "Jane Recruiter <jane@acme.com>",
                "subject": "[acme · hire] — inbound", "tier": 4,
                "reply_to": "replies@acme.com",
            }
        ],
    }
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(receipt), encoding="utf-8")

    ledger = build(str(tmp_path))
    ob = next(o for o in ledger["obligations"] if o["cls"] == "inbound-lead-hire")
    assert ob["reply_to"] == "replies@acme.com"


# --- safe_intent stamping: first-touch only, ≤5 per class per run, never on linkedin ---

def _hire_row(i):
    return {
        "id": f"h{i}", "action": "fire",
        "sender": f"Recruiter{i} <r{i}@agency{i}.com>",
        "subject": f"[repo{i} · hire] — inbound", "tier": 4,
    }


def test_safe_intent_stamped_on_first_touch_inbound_leads(tmp_path):
    rows = [
        _hire_row(0),
        {"id": "d1", "action": "fire", "sender": "Client <c@corp.com>",
         "subject": "[repo · deploy] — inbound", "tier": 4},
        {"id": "li", "action": "fire", "sender": "LinkedIn <noreply@linkedin.com>",
         "subject": "You have a new InMail message", "tier": 4,
         "headers": "List-Unsubscribe: <x>"},
    ]
    receipt = {"result": {"account": "acct", "total": len(rows)}, "rows": rows}
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(receipt), encoding="utf-8")

    ledger = build(str(tmp_path))
    by_cls = {o["cls"]: o for o in ledger["obligations"]}
    assert by_cls["inbound-lead-hire"]["safe_intent"] == "inbound-ack-hire"
    assert by_cls["inbound-lead-deploy"]["safe_intent"] == "inbound-ack-deploy"
    # NEVER inbound-linkedin — its sender is a noreply relay (structurally unsendable).
    assert "safe_intent" not in by_cls["inbound-linkedin"]


def test_safe_intent_capped_at_five_per_class_per_run(tmp_path):
    # 7 distinct hire leads → at most 5 may be stamped (class blast-radius cap).
    rows = [_hire_row(i) for i in range(7)]
    receipt = {"result": {"account": "acct", "total": len(rows)}, "rows": rows}
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(receipt), encoding="utf-8")

    ledger = build(str(tmp_path))
    hire = [o for o in ledger["obligations"] if o["cls"] == "inbound-lead-hire"]
    stamped = [o for o in hire if o.get("safe_intent")]
    assert len(hire) == 7
    assert len(stamped) == 5


def test_safe_intent_not_restamped_after_ack_sent(tmp_path):
    # First-touch = the obligation's _ob_key is NOT yet in drafts_sent.json. Once the SAFE ack
    # has fired (its key recorded), a later build must NOT re-stamp it (so it never re-sends).
    rows = [_hire_row(1)]
    receipt = {"result": {"account": "acct", "total": 1}, "rows": rows}
    (tmp_path / "inbox_sweep-acct.json").write_text(json.dumps(receipt), encoding="utf-8")

    first = build(str(tmp_path))
    ob = next(o for o in first["obligations"] if o["cls"] == "inbound-lead-hire")
    assert ob["safe_intent"] == "inbound-ack-hire"

    # Record its ack as sent (send_drafts.py's audit/drafts_sent.json, keyed by _ob_key).
    (tmp_path / "drafts_sent.json").write_text(json.dumps([_ob_key(ob)]), encoding="utf-8")

    second = build(str(tmp_path))
    ob2 = next(o for o in second["obligations"] if o["cls"] == "inbound-lead-hire")
    assert "safe_intent" not in ob2


# --- inbox_sweep: Reply-To captured from message headers into the receipt row ---

def test_inbox_sweep_captures_reply_to_into_row():
    # A message whose captured headers carry a reply-to → the receipt row gets a top-level
    # reply_to field (what obligations_build / draft_writer prefer over the raw From).
    msg = EmailMessage(
        id="1", sender="Jane Recruiter <jane@acme.com>",
        subject="[acme · hire] — inbound",
        headers={"reply-to": "replies@acme.com"},
    )
    rows = classify_inbox(_FakeProvider([msg]), "INBOX", limit=10)
    assert rows[0]["reply_to"] == "replies@acme.com"


def test_inbox_sweep_omits_reply_to_when_absent():
    # No reply-to header → no reply_to key (fail-open; the From is used downstream).
    msg = EmailMessage(
        id="2", sender="Bob Client <bob@corp.com>",
        subject="[corp · deploy] — inbound",
        headers={"list-unsubscribe": "<x>"},
    )
    rows = classify_inbox(_FakeProvider([msg]), "INBOX", limit=10)
    assert "reply_to" not in rows[0]
