# Evidence workflow

An obligation is independent work; a message is evidence. Flag color is a workflow view, and archiving is not completion.

## Commands

```sh
umail mail-observe --account ACCOUNT --output PRIVATE/observation.json --shadow PRIVATE/shadow.json
umail mail-research --input PRIVATE/observation.json --output PRIVATE/research.json --thread-limit 25
umail mail-research --input PRIVATE/observation.json --resume PRIVATE/research.json --output PRIVATE/research.json --thread-limit 25
umail mail-workflow plan --input PRIVATE/evidence.json --output PRIVATE/obligations.json
umail mail-workflow reconcile --input PRIVATE/evidence.json --output PRIVATE/obligations.json
umail mail-workflow flag-candidates --input PRIVATE/obligations.json --flag-plan PRIVATE/plan.json --output PRIVATE/candidates.json
umail mail-workflow import-legacy --input PRIVATE/legacy-ledger.json --output PRIVATE/evidence.json
umail mail-workflow benchmark --input tests/fixtures/obligations/benchmark.json --output PRIVATE/benchmark.json
umail flags verify --plan PRIVATE/plan.json --ledger PRIVATE/transactions.jsonl --output PRIVATE/verification.json
umail flags reconcile --plan PRIVATE/plan.json --ledger PRIVATE/transactions.jsonl --output PRIVATE/verification.json
```

`flags plan`, `flags human-canary-approve`, `flags apply` and `flags rollback` retain the preserved versioned contracts and activation restrictions; consult their `--help`. Verification is observation only: it never unfreezes a transaction or authorizes replay.

Research continuation validates the observation digest and prior artifact hash before
advancing the candidate cursor. Each invocation attempts at most 25 additional
candidates. Failed reads stay recorded as blockers even after the cursor advances;
advancing the cursor does not resolve their questions. Old artifacts without a
continuation hash cannot be resumed. Interrupted seeds may be read again, and long
threads still require a provider continuation capability. These limits prevent a
candidate checkpoint from being mistaken for complete correspondence research.

Outlook connections require `account=` or `OUTLOOK_ACCOUNT`. Cached-token selection
and authenticated Graph identity must match that account. Every Graph request uses
the [immutable ID preference](https://learn.microsoft.com/en-us/graph/outlook-immutable-id).
Existing stored mutable IDs are not migrated by this change; regenerate observations
before preparing plans. Identity verification may require authentication with the
service; no additional scopes are automatically granted.

`flag-candidates` is the explicit compatibility bridge: it binds independent obligation records to exact preserved native mutation ids and suggests target colors for review. It does not rewrite the preserved plan, sign approvals, or bypass the canary gate. Unknown siblings keep REVIEW visible; coverage gaps and overrides block selection readiness.

`archive-workflow observe|plan|apply|verify|reconcile|rollback` uses the native archive
adapters. Select `--provider` and `--account` explicitly. `--guard-evidence` binds
reviewed obligations to the persistent Mail.app override store. A production v2
plan contains fresh coverage evidence and positive preservation observations.
Canary approval binds the plan, policy, operator selection receipt, and 1–3 exact
mutation IDs. Native adapters report `applied`, `not_dispatched`, or `ambiguous`;
they use local writer locks and server readback, without claiming an atomic server
revision precondition. Verification never repeats a write.

`mail-inventory` resumes all discovered folders with account identity, memberships,
threading headers, scan boundaries, and page receipts. `mail-corpus` reconstructs
account-scoped RFC correspondence from complete inventories. Passing that corpus
to `mail-research` resumes long threads between individual reads. Each unit retains
the 25-thread and 20-read limits; `--work-units` shares one 600-second ceiling.
Collected messages and attachments still require obligation and relevance review.

`mail-workflow review-record` records that review against every message receipt and
attachment disposition in a complete thread. Revisions preserve prior evidence and
must explicitly supersede it. `archive-coverage` produces coverage from those
reviews; production archive plans must retain the entire reviewed obligation set.
Both operations take `--corpus`, `--research`, and `--reviews` private paths.

For Outlook mail-only consent, profile access can be unavailable while mail access
works. Identity verification then compares the authenticated Inbox with the
explicit `/users/{account}/mailFolders/inbox` resource using immutable IDs. Different
mailboxes fail the check; no extra profile permission is requested. See the
[Graph folder resource](https://learn.microsoft.com/en-us/graph/api/mailfolder-get?view=graph-rest-1.0).

`mail-github-evidence discover --research PATH` reads completed collections.
`discover --inventory PATH` streams exact notification identities from validated
inventory pages, so unrelated body collection does not delay GitHub intake. Either
source option can repeat across accounts. Native identity, every folder membership,
and the source receipt remain attached to each signal. Header discovery alone is
not complete correspondence coverage. `resolve` resumes authenticated object-specific
research; unresolved or failed objects cannot establish archive eligibility.

## Evidence contract and compatibility

`uma.obligation_evidence.v1` contains `obligations`, optional `coverage_gaps`, and optional versioned flag `verification_receipts`. Each obligation has an id, title, thread id, account-scoped message identities, evidence events and unresolved questions. Each event binds its exact obligation and message, occurrence/observation times, source, proof reference, fact, next actor/action and any evidenced deadline.

The derived `uma.obligations.v2` view records posture independently of lifecycle, evidence freshness, review age, archive eligibility and verification status. Assertions are structured reviewed evidence, not extraction of authority from email text. A proof string alone never grants mutation authority. A complete current thread is needed before using a historical Sent reply as current state.

Legacy sender/domain aggregates stay supported by their existing consumers. The explicit adapter imports each into REVIEW with an identity-resolution question. It does not reinterpret a boolean star, subject stem, draft, or old answered-key marker as a seven-state disposition. The v2 evidence workflow is additive; legacy scheduled consumers have not been migrated or activated by this change.

Protected mail, current human overrides, coverage gaps and active sibling obligations block archive eligibility. Missing archive audit observations are represented as an unknown metric, not zero incorrect archives. Read-only research collects at most 25 candidates and 20 message records per candidate; Sent matches require exact RFC threading evidence. Attachments and missing inbound chronology remain explicit evidence work.

## Private ops view

Set `UMA_OBLIGATIONS_PATH` to a derived v2 artifact and use the existing `UMA_OPS_TOKEN` authentication. `GET /v1/ops/obligations` requires authentication even when other summaries are public, validates the artifact hash, and exposes the private view in `web/ops.html`. There is no new HTTP mutation endpoint. No hosting or scheduler configuration is changed.

## One-time Mail sidebar setup

Expand **Flagged** in Mail's sidebar. Click a flag name, click it again, and type its semantic name:

| Native color | Sidebar name | Meaning |
| --- | --- | --- |
| Red | NOW | Evidenced consequential deadline requires immediate action |
| Orange | ACTION | You own the next step |
| Yellow | WAITING | Correspondence supports another party owning the next step |
| Green | SCHEDULED | Confirmed future commitment |
| Blue | REFERENCE | Supports active work |
| Purple | REVIEW | Evidence or judgment remains unresolved |
| Gray | LATER | Deliberate deferral with a checkpoint |

Apple documents sidebar renaming in its [Mail flag guide](https://support.apple.com/guide/mail/mark-emails-to-revisit-later-mlhlp1052/mac). The semantic names are visible in Mail. Native reordering did not settle into the intended order; the same seven views are now verified in ordered Favorites. The native Flagged group is collapsed to avoid duplicate presentation. Settled counts and presentation evidence are recorded in the redacted rollout receipts, separately from message mutation verification.

## Sending boundary

Only the operator literally clicking **Send in Mail.app** authorizes transmission.
The assistant must never click Send. `config/mail-execution-policy.json` enforces
this boundary in legacy and headless SMTP entrypoints. Old receipts, `--fire`, and
armed environment variables cannot override it. Draft preparation remains separate.

## Validation and policy changes

```sh
uv sync --extra dev --extra api --extra outlook --extra mcp
.venv/bin/python -m pytest -q
```

Reviewed corrections belong in redacted regression fixtures. Benchmark cases pin `as_of` so versions evaluate the same chronology. A passing report proposes a policy change; it never promotes one, learns new authority, or activates an account policy automatically.
