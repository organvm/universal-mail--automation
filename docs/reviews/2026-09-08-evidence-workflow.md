# Evidence workflow implementation receipt

## Scope and verification

The preserved safety components were restored selectively from `fb8dc1d5`, without merging its old branch or bypassing canary gates. The final full suite in the locked Python 3.11 project environment passed **1,604 tests with no skips**. Changed runtime modules pass Ruff F/E9 checks; a broader scan reports 16 existing unused imports in untouched modules. Dashboard JavaScript syntax checks and `uv build --wheel --quiet` passed. An initial non-isolated build lacked setuptools in the runtime environment; the standard isolated build succeeded. Test warnings concern existing FastAPI/Starlette test-client deprecation and the existing Pydantic `schema` field name.

## Live scope accounting

| Observation | Result |
| --- | --- |
| Account scope | Gmail and iCloud |
| Scoped scans | 180 |
| Distinct observed account/native-message identities | 435 |
| Incomplete surfaces | 4 |
| Research candidates | 25 |
| Research message records, including relevant Sent | 32 |
| Remaining candidates outside this research run | 410 |
| Mailbox writes | 0 |
| Scheduler activations / account changes / sends | 0 |

The incomplete surfaces are a Gmail All Mail timeout and three unresolved nested Gmail mailbox references. They remain in private receipt coverage gaps; an empty result was not promoted to a complete scan. Observation counts describe provisional evidence records, not independently resolved substantive obligations. Earlier user-supplied mutation counts were not recertified.

## Durable private evidence

Private, ignored mode-0600 JSON lives under `audit/evidence-workflow/`: `observation.json`, `research.json`, `reviewed-evidence.json`, `reviewed-shadow.json`, and `benchmark.json`. The session-specific annotations are private evidence work, never embedded as classifier rules. The tracked code, synthetic fixtures, schemas and this redacted receipt are the public work products.

## Open gates

- **UMA-EVIDENCE-COVERAGE:** complete missing inbound chronology and further bounded research; repair or independently resolve the four inaccessible observation surfaces.
- **UMA-EXTERNAL-AUTH:** the installed read-only cloud billing resolver has no active authenticated account; other account-specific status questions require their owning read-only surfaces.
- **UMA-CANARY-SELECTION:** exact evidence-backed 1–3-message selections and restored activation approvals are absent. No live flag canary or bounded auto-apply policy was activated.
- **UMA-ARCHIVE-ADAPTERS:** server-proof contracts and a conditional transaction engine pass offline tests. Gmail/iCloud native conditional adapters are not wired; live archive commands explicitly block. Outlook/IMAP parity remains a later capability gate.
- **UMA-SIDEBAR-ORDER:** manual semantic naming is documented; native order has not been demonstrated.

Owners: UMA CLI/provider workflow for capability and coverage work; the operator for personal decisions and explicit canary selection; each external account's authorization lifecycle for authenticated reads. Next checkpoint is the next on-demand workflow run after the required evidence or authority is available. No claim of completed inbox healing, deployed service, or policy promotion is made.
