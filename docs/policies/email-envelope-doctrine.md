# Email Envelope Doctrine: Brief, Actionable, Zero-Fluff

## 1. Executive Summary

Email has suffered a catastrophic degradation in the era of generative AI. Long, winding messages saturated with boilerplate pleasantries, throat-clearing apologies, and regurgitated bullet points cause cognitive fatigue and decision paralysis. They reek of automated, low-investment text generation that shifts the entire cognitive burden of parsing onto the recipient.

This doctrine establishes the **Evolved Ideal Form** for all outbound correspondence generated or transmitted by Universal Mail Automation (UMA):

> **Email is an Actionable Transmittal Envelope, not a Filing Cabinet.**
> Depth, exhaustive context, research, and data tables live in **attachments and linked artifacts**.
> The email body exists solely to deliver context, a concise payload/decision, and a clear next step in **under 100 words**.

---

## 2. The Tri-Vector Communication Topology

Human collaboration operates across three distinct media channels. Blurring their boundaries destroys communication efficiency.

```
+-----------------------------------------------------------------------------+
| 1. INSTANT MESSAGING / CHAT (Slack, SMS, iMessage)                          |
|    • Length: 5 - 25 words (sub-cognitive ping)                              |
|    • Nature: Synchronous / high-interruption / ephemeral                    |
|    • Role: Real-time logistics, urgent blocks ("Starting call in 2m")       |
+-----------------------------------------------------------------------------+
                                       |
                                       v
+-----------------------------------------------------------------------------+
| 2. EMAIL ENVELOPE (The Transmittal Slip)                                    |
|    • Length: 30 - 90 words (Target), 120 words (Soft Max), 150 words (Hard) |
|    • Nature: Asynchronous, courteous, high-density signaling                |
|    • Reading Time: 10 - 20 seconds (fits on a single phone screen)          |
|    • Role: Context + Core Payload + Explicit Call to Action / Decision      |
+-----------------------------------------------------------------------------+
                                       |
                                       | points to / accompanies
                                       v
+-----------------------------------------------------------------------------+
| 3. ATTACHMENTS & ARTIFACTS (PDFs, Markdown Specs, Reports, Spreadsheets)    |
|    • Length: 150 - 5,000+ words                                             |
|    • Nature: Structured, referenceable, immutable, versioned                |
|    • Reading Time: 2 - 30+ minutes (read when recipient enters deep focus)  |
|    • Role: Full narrative, data tables, proof, multi-topic analysis         |
+-----------------------------------------------------------------------------+
```

---

## 3. The 4-Part Envelope Architecture

Every outgoing email body must adhere to this lean structure:

1. **Direct Salutation**: Address the recipient by name (`Hi Alex,`). No decorative titles or rambling openings.
2. **Context Trigger (1 sentence)**: Why this email exists (`Thanks for sending over the Q3 product metrics yesterday.`).
3. **Core Payload / Decision (1–2 sentences)**: The central answer, status update, or finding. Zero fluff.
4. **Call to Action (CTA) / Next Step (1 sentence)**: Specific ask, deadline, or confirmation (`Can you confirm if Option B works for your team by Thursday?`). If an attachment is included, point directly to it (`The full breakdown is attached in report.pdf.`).
5. **Clean Sign-off**: Professional closing (`Best,\nAnthony`).

---

## 4. Quantitative Thresholds & Invariants

| Metric | Target / Ideal | Soft Warning Ceiling | Hard Gate (Rejection) |
| :--- | :--- | :--- | :--- |
| **Word Count** | 30 – 90 words | 120 words | > 150 words |
| **Character Count** | 200 – 600 chars | 700 chars | > 800 chars |
| **Paragraph Count** | 2 – 3 paragraphs | 4 paragraphs | > 4 paragraphs |
| **Call to Action** | Exactly 1 explicit ask | 2 asks | > 2 asks (split thread) |
| **Reading Time** | < 25 seconds | < 35 seconds | > 45 seconds |

If a message requires more than 120 words or multiple complex sub-sections, it **must be factored into an attachment or artifact**, with the email body serving as the transmittal note.

---

## 5. Anti-AI-Slop & Banned Clichés

The following boilerplate patterns are strictly banned in automated drafts and send lanes:

| Banned AI Fluff Phrase | Required Evolved Alternative |
| :--- | :--- |
| *"I hope this email finds you well"* | Omit entirely. Start directly with the context or greeting. |
| *"I am writing to follow up on..."* | *"Following up on [Subject]..."* or state the point immediately. |
| *"Please do not hesitate to reach out if you have any questions"* | Omit, or specify an explicit deadline/next step. |
| *"At your earliest convenience"* | State the exact time frame needed (`by Friday at 3pm ET`). |
| *"I wanted to touch base regarding..."* | State the status or ask directly. |
| *"Allow me to introduce myself..."* | State your role and mission in 1 sentence. |
| *"I hope you are having a wonderful week"* | Omit entirely. |

---

## 6. System Enforcement

This doctrine is enforced at three levels across UMA:
1. **Draft Generation (`core/mail_draft_package.py` & `core/voice.py`)**: Draft synthesis enforces word and character caps, refusing to generate bloated essay drafts.
2. **Policy Engine (`core/envelope_policy.py`)**: Evaluates outbound content against word limits, paragraph limits, and fluff detection patterns.
3. **Send Lane Gating (`mail_send_safety.py` & `mail_send.py`)**: Fails closed if an outbound email body exceeds hard limits or contains banned AI fluff, unless explicitly overridden with `--skip-envelope-check`.
