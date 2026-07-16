#!/usr/bin/env python3
"""protocols.py — the outbound decision cascade: protocol → precedent → exploration.

Given a surfaced obligation (a "fire" row: sender, subject, label, tier), derive the
KNOWN NEXT STEP without a network call or an LLM. Three rungs, tried in order:

    1. PROTOCOL    — a recurring class with a codified next step + draft intent
                     (loan-default, billing-decline, fraud-alert, kyc, legal-sign …).
    2. PRECEDENT   — no protocol, but a real human / known correspondent → reply by
                     precedent (a person who wrote you is owed a human reply).
    3. EXPLORATION — unknown → a research/verify next step until the action is certain.

This module only DECIDES the rung and emits the concrete, owned next step (+ an
optional draft intent). It never sends or mutates mail. The hard ideal-form synthesis
for the actual reply text — when protocol+precedent are silent — is left to a keyless
``claude -p`` pass downstream; this is the deterministic spine that says which rung
applies and what the human's single next move is.

Design notes:
- Every obligation is owned by HIM ("yours") — the system surfaces, it never acts.
- `verify` marks a class that is phishing-prone (fraud/credential-change notices): the
  next step leads with "confirm the sender is real" before any action, so a spoof can
  never steer a real action. The raw display name never drives the decision.
- Priorities are derived from consequence (money/legal/security > infra > marketing),
  not from the sender's brand — a generic "payment failed" outranks a recruiter.
"""

import re
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, TypedDict, Union

class ProtocolDef(TypedDict):
    cls: str
    match: re.Pattern[str]
    priority: int
    verify_first: bool
    requires_reply: bool
    next_step: str
    draft_hint: Optional[str]
    tags: List[str]


@dataclass
class Obligation:
    """One derived next-step for a surfaced fire. Provenance (account/id) is attached
    by the builder; this is the decision layer."""
    cls: str                       # protocol class id (or "precedent"/"exploration")
    rung: str                      # "protocol" | "precedent" | "exploration"
    priority: int                  # 0–100, by consequence
    next_step: str                 # the single concrete move he makes
    why: str                       # one line: which rung fired and on what signal
    owner: str = "yours"
    verify_first: bool = False     # phishing-prone → confirm sender before acting
    requires_reply: bool = False   # is a human/outbound reply owed?
    draft_hint: Optional[str] = None  # seed intent for a draft-only writer (never sent)
    tags: List[str] = field(default_factory=list)


def _addr(sender: str) -> str:
    return (sender.split("<")[-1].rstrip(">").strip().lower()
            if "<" in sender else (sender or "").strip().lower())


def _looks_human(sender: str) -> bool:
    """A real person, not a role/brand mailbox. Mirrors inbox_sweep.looks_human but kept
    local so this module is import-free and reusable headless."""
    addr = _addr(sender)
    role = ("noreply", "no-reply", "donotreply", "do-not-reply", "notification",
            "notifications", "alert", "alerts", "support", "info", "hello", "team",
            "news", "marketing", "auto", "mailer", "updates", "account", "billing",
            "service", "services", "reply", "sales", "contact", "admin", "store",
            "order", "payments", "reviews", "feedback", "survey", "invoice", "notify")
    if any(k in addr for k in role):
        return False
    name = (sender or "").split("<")[0].strip().strip('"')
    parts = [p for p in re.split(r"\s+", name) if p]
    brandish = any(b in name.lower() for b in
                   ("via", "inc", "llc", "bank", "card", "health", "deals", "store",
                    "finance", "co.", "labs", "group", "platform", "pbc"))
    return len(parts) >= 2 and not name.isupper() and "@" not in name and not brandish


# RFC 2369 / RFC 3834 / RFC 5322 bulk-mail markers. Their PRESENCE (any of them) means
# the message was emitted by a mailing list / bulk sender / auto-responder — by definition
# NOT a personal message, so NO personal reply is ever owed. This is the header-based,
# provider-agnostic rule that replaces sender-name guessing for the newsletter/transactional
# storm: `List-Unsubscribe` and `List-Id` mark list/bulk mail; `Precedence: bulk|list|junk|
# auto_reply` marks bulk/automated mail; `Auto-Submitted` (RFC 3834, anything but "no") marks
# an auto-generated message. The raw header VALUES never steer a real action — only presence.
_BULK_HEADER_KEYS = ("list-unsubscribe", "list-id", "list-post")
_PRECEDENCE_BULK = re.compile(r"(?i)\b(bulk|list|junk|auto[_\-]?reply|auto[_\-]?submitted)\b")

# The exact header field NAMES a provider must capture at list time for the bulk gate to
# fire. Single source of truth so the fetch layer (providers) and the classifier stay in
# lockstep — a provider grabs only these (never whole bodies), keeping the fetch cheap.
BULK_SIGNAL_HEADERS = (
    "List-Unsubscribe", "List-Id", "List-Post", "Precedence", "Auto-Submitted",
)

# The full header set the fetch layer captures at list time — the bulk-signal headers PLUS
# Reply-To. Reply-To is NOT a bulk signal (it never feeds is_bulk_mail); it is captured so a
# draft/reply can prefer the address the sender actually wants replies at (InMail relays and
# many role senders set a distinct Reply-To that threads back correctly). Single source of
# truth for the provider's cheap header grab — still only these named fields, never the body.
CAPTURE_HEADERS = BULK_SIGNAL_HEADERS + ("Reply-To",)


def _normalize_headers(headers: Union[str, "Mapping[str, Any]", None]) -> dict:
    """Coerce headers (a raw RFC-822 header block, or a name→value mapping) into a
    lower-cased {name: value} dict. Fail-open: unparseable input → {} (no suppression)."""
    if not headers:
        return {}
    if isinstance(headers, Mapping):
        return {str(k).strip().lower(): str(v) for k, v in headers.items()}
    out: dict = {}
    # Raw header block: fold RFC 5322 continuation lines (leading whitespace), then split
    # each logical line at the first colon. Later duplicates win (harmless for presence).
    logical: List[str] = []
    for line in str(headers).splitlines():
        if line[:1] in (" ", "\t") and logical:
            logical[-1] += " " + line.strip()
        else:
            logical.append(line)
    for line in logical:
        if ":" in line:
            name, _, val = line.partition(":")
            out[name.strip().lower()] = val.strip()
    return out


def is_bulk_mail(headers: Union[str, "Mapping[str, Any]", None]) -> bool:
    """True when the message carries standard bulk/list/auto-response headers — i.e. it was
    sent to a list or by a machine, never as a personal message. Header-based and
    domain-agnostic: no sender allow/block list. Absent headers → False (fail-open: an
    unheadered message falls through to the normal precedent/exploration cascade)."""
    hdrs = _normalize_headers(headers)
    if not hdrs:
        return False
    if any(k in hdrs for k in _BULK_HEADER_KEYS):
        return True
    prec = hdrs.get("precedence", "")
    if prec and _PRECEDENCE_BULK.search(prec):
        return True
    auto = hdrs.get("auto-submitted", "").strip().lower()
    if auto and auto != "no":
        return True
    return False


# Each protocol: a class id, a matcher over "sender subject snippet", a consequence
# priority, the codified next step, whether it needs sender-verification first, whether
# a reply is owed, and a draft intent (None = no outbound reply, just an action).
# Order matters: most-consequential / most-specific first (first match wins).
_PROTOCOLS: List[ProtocolDef] = [
    {
        "cls": "security-credential-change",
        "match": re.compile(r"(?ix) (fsa\s*id|password|sign[-\s]*in|login\s*code|"
                            r"allowed .*access|access to .*account|was changed|"
                            r"recovery|2fa|two[-\s]*factor|verification code)"),
        "priority": 95, "verify_first": True, "requires_reply": False,
        "next_step": "VERIFY you made this change. If NOT you: secure the account "
                     "immediately (change password, revoke sessions/third-party access).",
        "draft_hint": None,
        "tags": ["security"],
    },
    {
        "cls": "fraud-alert",
        "match": re.compile(r"(?ix) (fraud|unauthorized|unrecognized|suspicious|"
                            r"did you (make|try)|confirm you|security alert|locked)"),
        "priority": 90, "verify_first": True, "requires_reply": False,
        "next_step": "VERIFY the sender is genuine (fraud notices are heavily spoofed — "
                     "do NOT click links). If real, call the number on the back of the card.",
        "draft_hint": None,
        "tags": ["security", "money"],
    },
    {
        "cls": "loan-default",
        "match": re.compile(r"(?ix) (nelnet|studentaid|student\s*loan|fsa\b|"
                            r"default|garnish|wage|recertif|repayment plan|"
                            r"federal student aid)"),
        "priority": 88, "verify_first": False, "requires_reply": False,
        "next_step": "Log in at nelnet.studentaid.gov: check default status, recertify "
                     "the income-driven repayment plan, and set the lowest viable payment.",
        "draft_hint": None,
        "tags": ["money", "gov"],
    },
    {
        "cls": "billing-decline",
        "match": re.compile(r"(?ix) (payment (was )?(unsuccessful|declined|failed)|"
                            r"problem billing|billing problem|couldn.?t charge|"
                            r"unable to charge|past[-\s]*due|overdue|outstanding balance|"
                            r"access .*paused|subscription .*paused|update your (payment|"
                            r"information|card)|charge the credit card)"),
        "priority": 82, "verify_first": False, "requires_reply": False,
        "next_step": "Root cause is the card-0186 hold — resolve THAT first, then update "
                     "the payment method here. (Cascades to Anthropic / Google Cloud / GitHub.)",
        "draft_hint": None,
        "tags": ["money", "card-0186"],
    },
    {
        "cls": "kyc",
        "match": re.compile(r"(?ix) (kyc|verify your (identity|business)|stripe|"
                            r"take action to keep|keep things running|tax (id|info)|"
                            r"w-?9|onboarding|verification (required|needed))"),
        "priority": 78, "verify_first": False, "requires_reply": False,
        "next_step": "Provide the exact info requested. Note: Stripe KYC is blocked on the "
                     "dead LLC — prefer the individual monetization rail (Ko-fi/Lemon Squeezy).",
        "draft_hint": None,
        "tags": ["money", "product"],
    },
    {
        "cls": "legal-sign",
        "match": re.compile(r"(?ix) (docusign|sign(ed)? (this|the|a )?(document|proposal)|"
                            r"review and sign|mediation|mediator|settlement|proposal\.pdf|"
                            r"complete with docusign)"),
        "priority": 80, "verify_first": False, "requires_reply": False,
        "next_step": "Read the document fully before signing (legal). Sign only if the "
                     "terms are correct; otherwise note the objection and reply.",
        "draft_hint": None,
        "tags": ["legal"],
    },
    # ── Inbound-lead family (the positioning surfaces are the lure; these route the bite) ──
    # A warm inbound — a recruiter/client who wrote FIRST. requires_reply=True, but NO
    # money/legal/security tags: an opportunity is low-stakes SAFE-tier eligible (an offer
    # is DECIDED, not held — held is for legal/money), so its tag set is deliberately
    # {opportunity, …} only. Sits BELOW every consequential class (security/fraud/loan/
    # billing/kyc/legal-sign) so a spoofed "opportunity" that also trips fraud/kyc is caught
    # there first (first-match-wins by list order). The draft encodes "no hoops": reply BY
    # EMAIL, external ATS/portal forms are not completed.
    # inbound-linkedin FIRST in the family: a linkedin.com InMail/connect/view is a real
    # inbound but arrives through a noreply relay (structurally unsendable), so it must route
    # HERE, not to inbound-lead-hire (whose broad "InMail" token would otherwise swallow it).
    # Lookaheads scan the whole haystack ("sender subject snippet label"): host linkedin.com
    # AND a message signal, EXCLUDING the job-alert/jobs-you-may/network-digest blasts (which
    # are not a person writing you). Classifies DESPITE List-Unsubscribe + a noreply sender —
    # that is exactly the point: the protocol match outranks the bulk-header gate.
    {
        "cls": "inbound-linkedin",
        "match": re.compile(r"(?ix) ((?=.*linkedin\.com)"
                            r"(?=.*(in[\s-]?mail|sent\s*you\s*a\s*message|"
                            r"wants\s*to\s*connect|viewed\s*your\s*profile))"
                            r"(?!.*(job\s*alert|jobs\s*you\s*may|hiring\s*in\s*your\s*network)))"),
        "priority": 70, "verify_first": False, "requires_reply": True,
        "next_step": "A LinkedIn inbound (InMail / connect / profile-view) — real signal "
                     "arriving through a noreply relay. Reply IN LinkedIn, or steer it to an "
                     "email path so the thread lives where you can act on it.",
        "draft_hint": "Thanks for the note. I keep opportunity conversations in email — "
                      "could you send the details to my address, or share yours and I'll "
                      "follow up there? Happy to go deeper once we're on email.",
        "tags": ["opportunity"],
    },
    {
        "cls": "inbound-lead-hire",
        "match": re.compile(r"(?ix) (\[[^\]]+·\s*hire\]\s*—?\s*inbound|"
                            r"recruiter|sourcing|talent\s*acquisition|talent\s*partner|"
                            r"role\s*at|position\s*at|hiring\s*for|"
                            r"opportunity\s*(at|with)|in[\s-]?mail)"),
        "priority": 76, "verify_first": False, "requires_reply": True,
        "next_step": "A recruiter wrote you FIRST — a warm hire lead. Reply by email, no "
                     "portal hoops: thank them, say you're interested, and ask for the JD, "
                     "comp range, process shape, and end-client (if agency), all by email.",
        "draft_hint": "Thanks — I'm interested. To move efficiently, could you send by email: "
                      "(1) the job description, (2) the comp range / rate, (3) the process shape "
                      "and how many rounds, and (4) the end-client if this is through an agency? "
                      "I keep hiring conversations in email and don't complete external ATS or "
                      "portal forms up front. Happy to share availability windows by email once I "
                      "have those.",
        "tags": ["opportunity", "career"],
    },
    {
        "cls": "inbound-lead-deploy",
        "match": re.compile(r"(?ix) (\[[^\]]+·\s*deploy\]|"
                            r"consult(ing|ation)?|engagement|proposal|quote|deploy|"
                            r"build\s*(this|it)\s*for)"),
        "priority": 76, "verify_first": False, "requires_reply": True,
        "next_step": "A prospective client wrote you FIRST — a warm deploy lead. Reply by "
                     "email: acknowledge, ask which engagement depth fits, and propose an "
                     "email-first next step.",
        "draft_hint": "Thanks for reaching out. To point you at the right shape, which "
                      "engagement depth fits: a data/API feed, a white-label surface, a "
                      "custom build, or an embedded engagement? Reply by email with a sentence "
                      "on the outcome you want and I'll send back a concrete next step — I keep "
                      "these conversations in email rather than external portals.",
        "tags": ["opportunity", "client"],
    },
    {
        "cls": "legal-correspondence",
        "match": re.compile(r"(?ix) (protected message|litigation|attorney|counsel|"
                            r"senator|government accountability|legal (unit|matter)|"
                            r"consent form|on behalf of)"),
        "priority": 68, "verify_first": False, "requires_reply": True,
        "next_step": "Read the message. If a response is owed, reply concisely confirming "
                     "receipt and the next channel/timeline.",
        "draft_hint": "Acknowledge receipt, confirm the next step or channel, keep it brief "
                      "and professional.",
        "tags": ["legal"],
    },
    {
        "cls": "registered-agent",
        "match": re.compile(r"(?ix) (legalzoom|registered agent|resign as your|"
                            r"reinstate|annual report|stay compliant)"),
        "priority": 58, "verify_first": False, "requires_reply": False,
        "next_step": "Decide: renew the registered agent OR let the LLC lapse "
                     "(the LLCs are dead — lapsing is likely correct, but confirm no live filing).",
        "draft_hint": None,
        "tags": ["money", "legal"],
    },
    {
        "cls": "subscription-renewal",
        "match": re.compile(r"(?ix) (paid\s+membership|membership\s+confirmation|"
                            r"membership\s+begins|membership\s+renewal|"
                            r"((paid\s+)?membership|subscription|plan) .*"
                            r"(renew|auto[-\s]*renew|charge|billed)|"
                            r"(renew|auto[-\s]*renew|charge|billed) .*"
                            r"((paid\s+)?membership|subscription|plan)|"
                            r"automatically renew|membership fee)"),
        "priority": 54, "verify_first": False, "requires_reply": False,
        "next_step": "Decide whether to keep the recurring subscription before the next "
                     "renewal date; cancel it if it is not actively useful.",
        "draft_hint": None,
        "tags": ["money", "subscription"],
    },
    {
        "cls": "domain-renewal",
        "match": re.compile(r"(?ix) (renew to keep|domain .*(expir|renew)|"
                            r"expiring soon|auto[-\s]*renew)"),
        "priority": 48, "verify_first": False, "requires_reply": False,
        "next_step": "Decide which domains are worth keeping; renew those, let the rest lapse.",
        "draft_hint": None,
        "tags": ["infra"],
    },
    {
        "cls": "infra-alarm",
        "match": re.compile(r"(?ix) (workers kv|free tier limit|routines? .*(paused|"
                            r"limit)|daily limit reached|cloud shell|deletion notice|"
                            r"quota|rate limit)"),
        "priority": 32, "verify_first": False, "requires_reply": False,
        "next_step": "Your own infra signal — the system self-heals. No action unless you "
                     "want to raise the limit / preserve the resource.",
        "draft_hint": None,
        "tags": ["infra", "self"],
    },
    {
        "cls": "app-update",
        "match": re.compile(r"(?ix) (update .*(app|macos)|security update for|"
                            r"please update any)"),
        "priority": 28, "verify_first": False, "requires_reply": False,
        "next_step": "Update the app if still in use; otherwise ignore (low risk, often "
                     "past deadline).",
        "draft_hint": None,
        "tags": ["maintenance"],
    },
]


def derive(sender: str, subject: str, label: str = "", tier: int = 4,
           snippet: str = "",
           headers: Union[str, "Mapping[str, Any]", None] = None) -> Obligation:
    """Run the cascade for one surfaced fire and return its owned next step.

    ``headers`` (optional) is the message's raw header block or a name→value mapping. A
    consequential PROTOCOL always wins first (a billing/fraud/legal notice matters even when
    it rides a list). Below the protocols, bulk/list/auto headers HARD-SUPPRESS the personal
    precedent rung: a newsletter or transactional receipt is never a personal reply owed,
    however human its From name looks. Absent headers → the cascade is unchanged (fail-open).
    """
    hay = f"{sender} {subject} {snippet} {label}"

    for p in _PROTOCOLS:
        if p["match"].search(hay):
            return Obligation(
                cls=p["cls"], rung="protocol", priority=p["priority"],
                next_step=p["next_step"], why=f"protocol:{p['cls']} matched the subject/sender",
                verify_first=p["verify_first"], requires_reply=p["requires_reply"],
                draft_hint=p["draft_hint"], tags=list(p["tags"]),
            )

    # Bulk gate — sits ABOVE precedent: standard list/bulk/auto headers mean the message was
    # sent to a list or by a machine, so no personal reply is owed. This is the root-cause fix
    # for the newsletter/transactional storm that a First-Last display name used to smuggle
    # through the precedent rung. Header-based and domain-agnostic — never a sender blocklist.
    if is_bulk_mail(headers):
        return Obligation(
            cls="bulk", rung="bulk", priority=20,
            next_step="Bulk/list/automated mail — no personal reply owed. Unsubscribe or "
                      "filter it if it's noise; otherwise ignore.",
            why="bulk: message carries list/bulk/auto headers (List-Unsubscribe / List-Id / "
                "Precedence: bulk), so it is not a personal reply owed",
            requires_reply=False,
            tags=["bulk", "no-reply"],
        )

    # Rung 2 — PRECEDENT: a real human wrote; a person is owed a human reply.
    if _looks_human(sender):
        return Obligation(
            cls="precedent", rung="precedent", priority=45,
            next_step="A real person wrote you — read and reply by precedent (match your "
                      "prior tone with this correspondent).",
            why="precedent: sender looks like a real human, no protocol matched",
            requires_reply=True,
            draft_hint="Reply in your own voice; answer their question or set the next step.",
            tags=["human"],
        )

    # Rung 3 — EXPLORATION: unknown signal → research until the action is certain.
    return Obligation(
        cls="exploration", rung="exploration", priority=35,
        next_step="Open and determine the required action (no protocol or precedent "
                  "matched — verify what, if anything, is owed).",
        why="exploration: no protocol or precedent matched",
        requires_reply=False,
        tags=["unknown"],
    )
