# `uma.mail.status.v1`

`uma.mail.status.v1` is UMA's public-safe daily proof receipt for mail triage.
It composes existing UMA reports instead of replacing them:

- `uma.ops.summary.v1` for the latest current mailbox surface.
- `uma.mail.intelligence.v1` for redacted historical intelligence.
- `uma.mail.historical_crosswalk.v1` for terminal accounting over every
  historical evidence item.
- Action, resolver, draft approval, and delivery ledgers when present.

The receipt omits raw sender, address, subject, body, snippet, headers, and
full source paths. It is safe to cite as an operator receipt.

## Required Mode Invariants

- `read_only: true`
- `mailbox_mutations: false`
- `sends: false`
- `deletes: false`
- `credential_changes: false`
- `approval_required_for_sends_deletes_credentials: true`
- `apply_means_real_mailbox_mutation: true`

The explicit processing states are:

- `read_only_seen`
- `classified`
- `queued`
- `mutated`
- `drafted`
- `sent`
- `resolved`
- `blocked`

## Historical Crosswalk

`uma.mail.historical_crosswalk.v1` assigns every historical evidence item one
terminal status:

- `resolved`
- `represented_in_ops`
- `stale_noop`
- `open`
- `blocked`
- `needs_human`

The reconciliation invariant is:

```text
source_messages = terminal_status_total + explicit_exclusions
```

`explicit_exclusions` must be listed; hidden exclusions are not allowed.

## CLI

```bash
umail mail-status \
  --ops-report ~/System/Reports/mail-triage/latest.json \
  --history ~/System/Reports/mail-history/latest.json

umail mail-historical-crosswalk \
  --history ~/System/Reports/mail-history/latest.json \
  --ops-report ~/System/Reports/mail-triage/latest.json \
  --require-reconciled
```
