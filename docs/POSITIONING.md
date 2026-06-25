# Universal Mail Automation — Internal Positioning

**STATUS: INTERNAL RECORD ONLY — NEVER PUBLISHED AS A SALES PAGE.**

This document is the strategic anchor for any inbound engagement routed from the
repository's front door (the CTA in `README.md`). It defines who pays, why the
engagement is high-ticket, and how a lead ascends the engagement-depth ladder.
We do not publish price figures publicly — **the production-grade weight of the
artifact is the price signal.** The engagement starts serious; there is no
Fiverr tier and no negotiation.

- [The Expensive Problem](#the-expensive-problem)
- [Who Pays](#who-pays)
- [Why It Is High-Ticket](#why-it-is-high-ticket)
- [The Engagement-Depth Ladder](#the-engagement-depth-ladder)

## The Expensive Problem

Inbox triage at scale is a trust problem disguised as a convenience problem.
Any team can write filters; almost no team will trust them, because the cost of
a single mistake is asymmetric. Auto-archive one wire-transfer alert, one legal
notice, one government deadline, one platform security warning, and the
automation has done more damage than a year of manual sorting ever saved. So the
choice collapses to two bad options:

1. **Keep it manual** — pay a salaried operator to triage hundreds of messages a
   day across a personal Gmail, a work Outlook, and an iCloud account that share
   no logic with each other.
2. **Run a black box** — adopt a filter that nobody can audit, and absorb the
   silent risk that something critical already left the inbox.

The bleed is real and recurring: lost hours, lost money on missed financial and
legal mail, and the standing anxiety of not knowing what the automation dropped.
Universal Mail Automation removes the asymmetry by making restraint *provable* —
a fail-closed protected-sender gate (`core` rules + the `test_protected_*`
enforcement suite) that never archives financial, legal, government, or platform
mail, paired with an independent, signed audit receipt (`api/receipts.py`,
`test_audit.py`, `test_receipts.py`) that refuses to report success if a
protected sender ever left the inbox.

## Who Pays

There are two high-ticket buyers for this capability, served by the *same* proof.

### A. The Operator Buyer (Deploy / Run / License)

- **Who:** Operations leaders, fractional COOs, agencies, and professional firms
  (legal, financial, real-estate) drowning in multi-account inbox triage but
  unable to risk a black-box filter losing a critical message. Also AI-agent and
  platform teams that need to act on a mailbox safely — served through the MCP
  server (`mcp_server/`) and the Agentic Commerce surface (`acp/`,
  `platform/checkout.py`) that let agents purchase and consume verified-safe
  triage runs without ever touching protected mail.
- **Why they pay:** They are not buying a clever filter; they are buying
  *liability-grade automation* — a fail-closed gate and a signed receipt that
  prove nothing critical was lost — and the salaried hours it returns. That proof
  is the difference between "we automated email" and "we can stand behind what we
  automated."
- **The signal:** White-glove deployment across their Gmail / Outlook / iCloud /
  Mail.app estate, a custom rules taxonomy and VIP-sender map, scheduled or
  on-demand runs, retained compliance receipts, and (for platform buyers) MCP
  tool wiring and metered agent access.

### B. The Talent Buyer (Hire)

- **Who:** VPs of Engineering, engineering leaders, and elite technical
  recruiters — at any company that needs production discipline, not just email
  vendors.
- **Why they pay:** This repository is one author's proof-of-work. It spans a
  provider-abstraction layer (`providers/base.py` with capability flags and
  per-provider adapters for the Gmail REST API, Microsoft Graph, IMAP, and
  AppleScript), a rules engine with Eisenhower tiering and time-based escalation
  (`core/rules.py`), crash-recovery state management (`core/state.py`), a
  fail-closed safety system with independent signed receipts, a Stripe-backed
  metered billing API (`api/billing.py`, `api/metering.py`, `api/plans.py`), an
  MCP server, an ACP agent-commerce stack, and a 29-module test suite. They are
  buying architectural breadth and the proven ability to ship production
  infrastructure unsupervised.
- **The signal:** Senior, staff, and principal engineering roles and technical
  leadership engagements.

## Why It Is High-Ticket

- **It is liability, not convenience.** The buyer is de-risking a process where a
  single silent failure costs real money or compliance standing. Provable
  restraint commands a premium that a "smart filter" never can.
- **The proof is independent and signed.** The audit receipt is designed to
  *refuse to lie* — it reports failure if a protected sender left the inbox. That
  is a property most automation cannot claim, and it is exactly what a serious
  buyer is willing to pay for.
- **The artifact is broad and real.** Four provider integrations behind one CLI,
  a 28-category rules engine, VIP overrides, time-based escalation, crash
  recovery, a metered billing API, an MCP server, an ACP agent-commerce surface,
  and a 29-module test suite. This is not a mockup; replicating it with cheap
  outsourced labor is not on the table.
- **No negotiation.** The engagement starts serious. The production-grade weight
  of the work is the price signal, and the front door routes straight to a
  conversation — not a quote form.

## The Engagement-Depth Ladder

Inbound leads from the `README` front door are routed through this ladder by need
and budget capability.

**Level 1 — Tactical Consult (Paid Discovery).**
An inbox-estate and rules audit: assess the buyer's current triage, providers,
and risk surface against this blueprint, and deliver a deployment roadmap. Paid
upfront; filters out tire-kickers immediately.

**Level 2 — Managed Deployment.**
Stand up a configured, branded instance across the buyer's Gmail / Outlook /
iCloud / Mail.app estate: custom rules taxonomy, VIP map, scheduled or on-demand
runs, the fail-closed protected-sender gate, and retained signed receipts for
compliance export. Deployment fee plus recurring managed-operation retainer.

**Level 3 — Custom Build / Integration.**
Net-new capability on top of the architecture: additional provider adapters
(Fastmail, ProtonMail, Yahoo), deep integration with the buyer's CRM, ticketing,
or compliance systems, and agent-commerce wiring through the MCP / ACP surfaces.
Project-based or retainer at premium consulting rates.

**Level 4 — Talent Acquisition (Hiring).**
The buyer wants the builder. They bypass the product and bring the engineer
in-house to architect and ship at a senior, staff, or principal level.
