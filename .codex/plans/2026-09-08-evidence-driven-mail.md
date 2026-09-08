# Evidence-driven mail implementation

Source of intent: the user's supplied Observe → evidence → state → authorize → apply → verify → reconcile plan.

## Implemented

- Selectively restored `fb8dc1d53bfcaa896ae84a09aa59d86ceb1b9634`: typed flags, pure native codecs, strict versioned plan/approval contracts, transaction journal, override store, scoped rollback, activation gates and adversarial tests. No old branch merge or activation override.
- Stable host routing for Python Mail entry points, no provider window activation, per-provider ten-minute lifetime. Flag writes compare envelope evidence, color and flagged status in the dispatch script. Readback checks both native properties immediately and at +2/+5 seconds, without repeating writes.
- A hard 25-mutation ceiling on flag apply, additional directory fsync for transaction journal durability, explicit read-only flag verify/reconcile commands.
- Additive obligation evidence contract with independent identities, actors, actions, deadlines, chronology, questions and checkpoints; conservative legacy adapter; protected/active-sibling archive vetoes; versioned deterministic benchmark.
- Conditional archive engine and CLI with full preflight, persisted intent, server-proof adapters, replay freeze and override-safe rollback. Legacy boolean archive adapters are explicitly unsupported by this engine.
- Read-only Inbox/flagged refresh and bounded RFC-linked Sent research; private authenticated ops view; removed hardcoded billing/entity explanations and blanket default ownership from protocols.

## Verified live, without mailbox writes

Two-account refresh: 435 distinct account-scoped native messages across 180 scans. Four incomplete surfaces remain; 25 research candidates produced 32 message records. All 435 provisional records remain accounted for. The private review has specific evidence questions for the 25 researched candidates; the other 410 require further bounded research. These provisional message records are not asserted to equal 435 distinct substantive obligations.

The stable host signature and deployment identity match. A read-only external billing resolver attempt was blocked by missing active authentication. No account configuration was changed.

## Remaining rollout gates

1. Resolve the four observation coverage gaps and finish further bounded research; reconstruct relevant inbound chronology as well as Sent before asserting current waiting/completion.
2. Implement and validate native conditional archive adapters for Gmail/iCloud. The new engine deliberately refuses the existing unqualified boolean archive methods. Server-proof validation functions and offline transactions are implemented; production archive parity is not complete.
3. Select exact 1–3-message flag canaries with sufficient current evidence, preserve the restored activation restrictions, and verify receipts before any account-scoped auto-apply policy. No canary selection or policy activation was made in this session.
4. Resolve authenticated external account questions through their owning services. Personal recognition/choice questions remain private review work.
5. Outlook/IMAP semantic parity and additional resolvers remain explicit capability gaps. No scheduler, public mutation route, send, delete, payment, signature, upload, or account change was activated.

Implementation and live rollout are separate. This change does not claim inbox healing or archive deployment complete.
