# Full Gap Coverage — Evidence-Driven Mail `feat/evidence-driven-mail`

**Date:** 2026-09-10 · **Branch:** `feat/evidence-driven-mail` · **Base:** `3337c76`  
**Source:** dirty-lane audit (10M+5U) + `2026-09-08-evidence-driven-mail.md:1` + `2026-09-08-four-account-rollout-v3.md:1` + `v5.json` + `docs/evidence-workflow.md:48`

## Gap inventory (verified at `2026-09-10T00:00Z`)

| Layer | Full coverage requires `docs/evidence-workflow.md:48` | Current | File:line |
|---|---|---|---|
| **Inventory** | 4× `manifest.json:complete:true`, validated `pages/*.json`, zero `failed`, `discovery_complete:true` (`core/mail_inventory.py:94`) | **CLOSED** `gmail 139/255437 7b51998f…`, `icloud 26/779 dd9f51fe…`, `outlook-* 12/38 12/44` | `audit/evidence-workflow/inventory/*/manifest.json`, `.codex/receipts/2026-09-08-mail-rollout-v5.json:8` |
| **4 Gmail gaps** | All Mail superset (116286) + 3 nested `Personal/*` (`docs/reviews/2026-09-08-evidence-workflow.md:21`, `.codex/plans/2026-09-08-four-account-rollout-v2.md:39`) | **CLOSED** `uma.coverage_gap_resolution.v1` sealed | `audit/evidence-workflow/gmail-gap-closure.json:13`, `.codex/receipts/2026-09-08-gmail-gap-closure.json:6` |
| **Corpus** | `build_corpus` (`core/corpus_research.py:19`) from 4 completes, RFC grouping, sealed `uma.research_corpus.v1` | **CLOSED** `gmail 66189/116286 6d5369a9…`, `icloud 736/779`, `outlook 38/44` | `audit/evidence-workflow/*-corpus.json` |
| **Research** | 25 threads + 20 reads per unit, 600s ceiling, resume validated, failed reads ≠ success (`core/corpus_research.py:113`, `core/inventory_cli.py:58`) | **OPEN** `gmail 342/66189 complete:false next_thread 351`; others `complete:true` | `audit/evidence-workflow/research-gmail/manifest.json:9`, `research-{icloud,outlook-*}` |
| **Review** | Every thread `uma.correspondence_review.v1` with all receipts + attachment dispositions + obligation↔identity closure (`core/corpus_review.py:71`) | **OPEN** 13/38 `outlook-dotted`, 0 elsewhere | `audit/evidence-workflow/reviews-outlook-dotted/*.json` |
| **Coverage artifact** | `uma.archive_coverage.v2` fresh 24h (`core/corpus_review.py:123`, `core/archive_transactions.py:59`) | **OPEN** not yet produced (`coverage_gaps` blocks `reconcile:204`) | `core/obligation_workflow.py:204` |
| **Flag bundle/plan** | `uma.flags.reviewed_evidence.v1` → `uma.flags.migration.plan.v6` with `plan[evidence]` in `plan_hash` (`core/evidence_flags.py:1`, `core/flag_workflow.py:4014`) + live `validate_live_evidence` (`core/flag_transactions.py:1019`) | **OPEN** code uncommitted (419+ / 940 new) | `core/evidence_flags.py`, `core/native_mailapp_binding.py:211` |
| **Archive guard/adapter** | `uma.archive_guard.v2` + `uma.archive_adapter.v2` (`local_writer_lock:true`, `verify:positive_preservation` `providers/archive_native.py:14`), `+5s` settlement freeze | **OPEN** code uncommitted, `canaries_approved:0` | `core/archive_guard.py:12`, `core/archive_transactions.py:97` |
| **Execution** | Canary 1–3 → scoped rollout 1–25 same-account 24h + no identity replay (`core/archive_transactions.py:120`); `human_click_in_mailapp` (`config/mail-execution-policy.json:4`) | **OPEN** | `v5.json:87 maintenance_migrated:false` |

Full coverage ≠ tests/manifests alone (`v3.md:44`).

## Dirty lane → clean (Phase 1, P0)

15 files net `-30` lines modified + `940` new — evidence-driven loop with zero direct mailbox mutation except attested transaction:

* `core/native_mailapp_binding.py:1` (211L) — `uma.native_mailapp_binding.v1` authenticated lineage, `imap_header_same_fetch`, `_transport_bytes` universal-newline only, `validate_binding(revalidate_sources)` .
* `core/evidence_flags.py:1` (309L) — `build_evidence_bundle`/`validate_evidence_bundle`/`decisions_for_snapshot`/`validate_live_evidence`.
* `core/maintenance.py:1` (289L) `intake(observation_only, mailbox_writes=0)` + `category_labels` + `dispatch_approved` allowlisted `archive-workflow apply | flags apply --canary --human-selected`; `mail-maintenance.py` shim.
* `core/archive_guard.py:12` guard v2, `core/archive_transactions.py:97` canary→rollout, `core/flag_workflow.py:47` plan v6, `core/flag_transactions.py:1019` live gate, `providers/mailapp.py:1125` `native_account_mapping`/`native_evidence_reference`/`read_native_source_ref`, `providers/archive_native.py:41` full-content proof, `inbox_sweep.py:416`/`gmail_imap_sweep.py:161`/`gmail_labeler.py:22`/`archive_sorted.py:80` demoted to `intake`.
* `tests/test_archive_rollout.py:1` (125L) — 5 tests (25-batch verified, 6-way invalid authority, identity replay, ambiguous freeze, rolled-back canary).

Action: `git add` 15 paths, `pytest -q` + `ruff`, commit `feat: evidence-driven mail v6 + intake + native binding` (atomic SHA per `CLAUDE.md:11`), then emit `.codex/receipts/2026-09-10-gap-coverage-baseline.json`.

## Coverage chain (Phases 2–8)

```
intake (maintenance.intake, seal, manifest, AdvisoryFileLock, 0 writes)
  → binding (native_mailapp_binding.build_binding, mailapp.native_*, X-GM-MSGID/Message-ID, flag -1..7)
  → evidence (evidence_flags.build_evidence_bundle, corpus+research+reviews, 24h fresh, attachment question veto)
  → plan (flag_workflow.build_plan v6, plan_hash covers evidence, protected→None)
  → approval (human-canary 1–3 v1 OR rollout 1–25 scoped 24h, policy_sha256==POLICY_HASH)
  → transaction (flag_transactions preflight + _apply_locked, archive_transactions _approved_selection+apply, +5s readback, replay/scope freeze)
  → verified receipt (v2 preservation, mailapp SOURCE framing UMA_SOURCE_V1)
```

Commands (`docs/evidence-workflow.md:7`): `mail-inventory --work-units 1-25 --max-pages 1-25` (shared 600s), `mail-corpus`, `mail-research --thread-limit 25 --resume`, `mail-workflow review-record`, `mail-workflow archive-coverage`, `mail-github-evidence discover|resolve --inventory|--research`.

## Phase plan

### Phase 2 — Re-validate inventories/corpus (no writes)
`core/mail_inventory.py:25 validated_pages` lineage check on 189 pages; `build_corpus` re-derivation asserts 4 `inventory_sha256` sources.

### Phase 3 — Research completion (wall-clock dominant)
`mail-research --resume` bursts until `next_thread==len(threads)` (`research-gmail` 342→66189). Each burst 25 threads × 20 reads, 600s ceiling shared (`core/inventory_cli.py:76`). Errors preserved as blockers (`core/corpus_research.py:182`). Icloud/outlook already complete — verify. GitHub `discover --inventory` (streaming) + `resolve` 2 unresolved/14 unavailable.

### Phase 4 — Review closure (human-time dominant)
`mail-workflow review-record` per thread: `message_receipts` chronology equality (`core/corpus_review.py:79`), `identities` exact (`:94`), `attachment_reviews` exhaustive with `relevant_read|not_relevant|question` + reason, obligation `id` uniqueness, proof binds receipt (`:91`). 0/736 icloud, 0/66189 gmail, 25/38 remaining outlook.

### Phase 5 — Coverage artifact (unblocks archive)
`mail-workflow archive-coverage --corpus --research --reviews` → `uma.archive_coverage.v2` (`inventory_receipts:[4×]`, `research_receipts` sorted, `message_keys` sorted, `reviewed_obligations:{id:sha256}`, `complete:true` 24h fresh `core/archive_transactions.py:67`). Any `coverage_gaps` ⇒ `reconcile:211` blocks `archive_eligible`.

### Phase 6 — Flag evidence→plan
`build_evidence_bundle` from bindings covering selected threads (exact identity, `validate_binding(revalidate_sources=True)`), `build_plan(snapshot, evidence)` → `v6`; `validate_evidence_plan` enforces mutation == proposal + proof + digests.

### Phase 7 — Guard + adapters
`build_guard_evidence(binding_paths, review_paths)` → `uma.archive_guard.v2`; adapters `GMAIL remove INBOX`, `Graph POST /move`, `IMAP MOVE` (`providers/archive_native.py:14` `CONTRACT`). Positive preservation + inbox absence verification.

### Phase 8 — Canaries then rollout (authorization gates)
`archive-workflow plan` 1–25 single-account (`core/archive_transactions.py:52`) → `human-canary-approve` 1–3 `uma.archive_canary_approval.v1` (`explicit_operator_selection`) → `apply`+`verify` (`verified_at` 24h, `adapter_contract==v2`) → `uma.archive_rollout_approval.v1` (`explicit_operator_rollout`, `plans:{hash->[ids]}`, `canary_receipt_sha256`, account scope, `scope_identity_requires_reconciliation`). Batches `apply` with lock-rechecked lineage; `dispatch_approved` frozen envelope on child loss.

### Phase 9 — Maintenance migration
All sweeps already `intake`-only (`gmail_labeler SYSTEM_LABELS=[]`, `inbox_sweep flag_only_gmail` retained as intake). `dispatch_approved` allowlisted CLI bounds 1–25. Migrate Limen beat + Domus `mail-triage` with cadence/bounds/kill-switch; prove next live cycle preserves result; redacted receipts in owning repos only, private `audit/evidence-workflow/*.json` (`0600`) stays untracked (`audit/*.jsonl`).

### Phase 10 — Parity & closeout
`uv sync --extra dev --extra api --extra outlook --extra mcp && .venv/bin/python -m pytest -q` (189+ scoped `v5.json:91`), `ruff`, `git diff --stat`. Atomic commits, `git push origin feat/evidence-driven-mail` only on explicit authorization (Rule #2). Final `.codex/receipts/2026-09-10-mail-rollout-v6.json` with `collected==len`, `reviewed==collected`, `archive_coverage.v2` hash, `canaries_approved`, `maintenance_migrated:true`.

## Risks & invariants

* Gmail 66k threads is strictly `research` wall-clock, not inventory gap — resume is bounded and retryable.
* `question` attachments block `archive_coverage` unless obligations carry questions (`core/corpus_review.py:103`).
* `validate_live_evidence(revalidate_sources=True)` rereads current review+binding at every write — stale canaries require re-approval.
* `whole_account superset` invariant (`gmail-gap-closure.json:8`) — never promote empty scan to `complete`.

## Next action

Approve plan → Phase 1 commit → baseline receipt → Phases 3–4 bursts interleaved (research + review) → coverage → plan → canaries.
