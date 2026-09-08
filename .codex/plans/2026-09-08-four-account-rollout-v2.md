# Complete the four-account mail rollout

Source: user-supplied rollout plan. Completion requires live verified coverage and
rollout for Gmail, iCloud, and both Outlook accounts, including historical archives.
This revision records partial implementation, not rollout completion.

## Policy and authorization

Retain messages and evidence. GitHub notifications may leave Inbox only after
object-specific work is verified resolved or superseded. Recorded tasks do not
qualify. Preserve independent obligations, later reopenings, provenance, and human
overrides. Sends, payments, account changes, and substantive GitHub actions require
separate concrete authorization.

Fresh private previews must identify 1–3 canaries per account and operation with
exact identities, evidence, intended changes, and rollback. Obtain explicit exact
selections after previews exist. Subsequent rollout requires scoped authorization
and batches of at most 25 mutations. Historical approvals cannot authorize it.

## Implemented in this revision

- Source-bound, hash-validated research candidate continuation with per-read private
  checkpoints, cumulative failure records, 25-candidate and 20-read limits.
- Mail.app flag writes no longer return on an immediate match; final +5-second
  observations determine settlement and third-state edits freeze the result.
- Archive verification rejects Gmail Trash/Spam and iCloud retained Inbox copies;
  apply observes through +5 seconds and read-only reconciliation is persisted.
- Explicit Outlook cached-account selection and authenticated identity checks,
  immutable-ID request preference, and 30-second Graph request timeouts.
- Synthetic tests for delayed reversion, candidate continuation and changed input,
  destructive memberships, retained Inbox copies, persisted reconciliation, both
  Outlook selections, wrong authenticated identity, and immutable-ID headers.

## Remaining implementation and acceptance work

1. Private complete four-account inventory, account/native identities, memberships,
   RFC headers, pagination, scan boundaries, and validated observation continuation.
   Include Sent and unflagged archives; inventory Junk/Trash separately. Resolve All
   Mail timeout and three nested Gmail lookup gaps with equivalent provider coverage.
2. Import or regenerate historical research manifests without fabricating lineage;
   finish the prior 410 candidates and additional discoveries. Implement long-thread
   continuation, inbound/Sent/archive chronology, attachment reads, and authenticated
   object-specific paginated GitHub and service evidence.
3. Implement a versioned evidence-to-executable-flag plan binding and native sidebar
   rename/order verification; use ordered Favorites if native reordering fails.
4. Carry complete coverage evidence into centralized archive eligibility. Implement
   account-bound Gmail, IMAP, and both Outlook archive adapters with truthful typed
   concurrency/dispatch contracts, positive preservation, and override-safe rollback.
   Gmail modify has no documented revision precondition; do not claim atomic CAS.
5. Establish exact authenticated service identities, create fresh canary previews,
   obtain selections, apply/verify canaries, then obtain scoped rollout authorization.
   Track researched, unresolved, approved, applied, and verified independently.
6. Route every legacy writer and managed mail-triage through canonical policy and
   transactions while preserving supported capabilities. Migrate the existing Limen
   mail beat and Domus managed source with their cadence, bounds, kill switch, and
   audit ownership; verify a subsequent live cycle.
7. Publish implementation and redacted receipts in their owning repositories. Keep
   all message contents, credentials, and private evidence outside tracked artifacts.

## Verification receipt

Final scoped run: 128 tests passed covering archive, research runtime, obligation
policy, Mail.app, and Outlook errors/identity. Scoped Ruff checks and git diff
whitespace validation passed.
No live mailbox mutations, account authentication, historical corpus scan, sidebar
changes, scheduler migration, or canary acceptance occurred in this revision.

Official references reviewed: Microsoft immutable Outlook identity guidance, Gmail
users.messages.modify reference, and Apple Mail flag/favorite-mailbox guides linked
in the supplied plan. Offline validation does not close any remaining live gate.
