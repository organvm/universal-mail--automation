# Branch constitution

Default branch: `main`
Policy: GitHub Flow on top of a small set of standing program lanes.
Worktrees: one worktree per active working branch. Do not pile unrelated WIP onto a lane.

## Standing branches (always exist)

| Branch | Purpose | Merge into | Green means |
| --- | --- | --- | --- |
| `main` | Production-true trunk. Always releasable. | tags / releases | required CI + tests pass; purpose invariants hold |
| `lane/verify` | Proof: correctness, tests, contracts, CI, reproducibility | `main` | verification suite is stricter or equally true |
| `lane/heal` | Repair of known broken or rotting behavior | `main` | previously failing paths pass, no regression |
| `lane/expand-providers` | Completing the stated coverage (Gmail, Outlook, Mail.app, SMS) | `main` | new coverage is verified, not merely sketched |
| `lane/evolve` | Structural improvement implied by current purpose | `main` | behavior preserved except for documented changes |

## Working branches (temporary)
Pattern: `work/<lane>/<short-intent>`
Also allowed: `feat|fix|chore|docs|test|hotfix/<short-intent>`

Rules:
- Cut from the lane you are advancing, or from `main` if the change is trunk-ready.
- One intention. Lifetime measured in days, not months.
- Merge by PR. Delete after merge.
- If blocked, park with a comment and keep the branch.

## Hotfix
`hotfix/<short-intent>` from `main` → PR to `main` → back-port to any living lanes that diverged.
