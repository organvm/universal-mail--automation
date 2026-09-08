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

`archive-workflow plan|apply|verify|reconcile|rollback` exposes the archive contracts. A plan input contains the complete obligation set and `archive_observations`. Observations bind exact identity, current Inbox membership, revision, protection/override checks and server evidence. A canary approval binds the plan and policy hashes, explicit operator selection receipt and 1–3 mutation ids. Production adapters must implement `observe_archive`, `archive_if_unchanged`, and `restore_archive_if_unchanged`; the existing Gmail/iCloud legacy methods do not satisfy that contract and remain blockers. No generic boolean archive fallback exists.

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
| Red | 01 NOW | Evidenced consequential deadline requires immediate action |
| Orange | 02 ACTION | You own the next step |
| Yellow | 03 WAITING | Correspondence supports another party owning the next step |
| Green | 04 SCHEDULED | Confirmed future commitment |
| Blue | 05 REFERENCE | Supports active work |
| Purple | 06 REVIEW | Evidence or judgment remains unresolved |
| Gray | 07 LATER | Deliberate deferral with a checkpoint |

Apple documents sidebar renaming in its [Mail flag guide](https://support.apple.com/guide/mail/mark-emails-to-revisit-later-mlhlp1052/mac). This documents names, not a promise that Mail will sort them numerically. Native ordering remains an unverified presentation capability. No assistant screen control or preference-file edits are involved.

## Validation and policy changes

```sh
uv sync --extra dev --extra api --extra outlook --extra mcp
.venv/bin/python -m pytest -q
```

Reviewed corrections belong in redacted regression fixtures. Benchmark cases pin `as_of` so versions evaluate the same chronology. A passing report proposes a policy change; it never promotes one, learns new authority, or activates an account policy automatically.
