# ACTIVE HANDOFF: Antigravity -> OpenCode

**Originating Agent**: Antigravity (Google DeepMind)
**Target Agent**: OpenCode (`opencode` v1.18.30)
**Timestamp**: 2026-09-10T20:42:30-04:00
**Working Directory**: `/Users/4jp/Workspace/4444J99/universal-mail--automation`
**Sibling Estate Vault**: `/Users/4jp/Workspace/4444J99/estate-vault`

---

## 1. Repository Status & Verification

- **Trunk (`main`)**: Clean, honest, fully verified.
- **Commit Head**: `58b6343` (Git history rewritten with `git-filter-repo` to permanently purge state leaks).
- **All Standing Lanes**: Fast-forwarded and synchronized to `origin`:
  - `lane/verify`
  - `lane/heal`
  - `lane/expand-providers`
  - `lane/evolve`
- **Verification Baselines**:
  - `pytest`: 1,670 passed, 4 skipped, 0 failures (100% green).
  - `ruff check --select E9,F63,F7,F82 .`: 0 errors.
  - `scripts/verify_vault_sync.py`: Live read-after-write test passed against `4444J99/estate-vault`.

---

## 2. Hard Architectural Constraints (DO NOT VIOLATE)

1. **Decoupled State**: 
   - Never write runtime state files (e.g. `labeler_state.json`, `mail_export.tsv`, `mail_report.md`) to the local git working tree.
   - All state management must route through `core/vault_sync.py` and `core/state.py`, which connect to `4444J99/estate-vault` via GitHub API.
2. **Standing Lanes Discipline**:
   - Follow `BRANCHES.md`. Standing branches must never diverge into uncoordinated feature sprawl. Work branches must branch from and fold back into these standing refs.
3. **Outbound Email & Envelope Doctrine**:
   - Strictly honor `docs/policies/email-envelope-doctrine.md`. Emails are transmittal envelopes, not essays (target 30-90 words, max 120 words).
4. **Secret Management**:
   - Zero credentials in repo. All environment secrets are sourced from 1Password (`~/.config/op/*.env.op.sh`). Pre-commit hook enforces strict secret scanning (use `# allow-secret` only for verified false-positive variable assignments).

---

## 3. Estate Status & Storage Quota Rebalancing

- **Organization Quota Lockout**: `organvm` was blocked due to ~60.6 GB of private storage usage.
- **Top Identified Storage Offenders**:
  - `organvm/arca`: ~51.08 GB
  - `organvm/session-meta`: ~4.55 GB
  - `organvm/render-second-amendment`: ~2.20 GB
  - `organvm/arca-g2`: ~538 MB
- **Current Actions In Flight**:
  - Transfer requests have been dispatched via GitHub API to transfer these repositories to the user's personal account `4444J99`.
  - Accepting these transfer invitations will drop `organvm` storage usage to < 500 MB and immediately restore GitHub Actions runners across all projects.

---

## 4. Workstreams & Immediate Next Missions

### Stream A: Email / Comms Engine (`universal-mail--automation`)
1. **Provider Scope Expansion (`lane/expand-providers`)**:
   - Expand IMAP and Exchange edge-case handling in `providers/imap.py` and `providers/outlook.py`.
   - Validate Apple Mail integration via `*.applescript`.
2. **Modular Synth Communication Router**:
   - Expand communications routing in `core/patchbay.py` and `core/models.py`.
3. **Web Dashboard (`web/`)**:
   - Ensure Next.js frontend at `web/` renders properly without local state dependency (fallback to API/Vault state).

### Stream B: Estate Vault (`4444J99/estate-vault`)
1. **SDK Rollout**:
   - Python client: `/Users/4jp/Workspace/4444J99/estate-vault/sdk/python/estate_vault.py`
   - Bash client: `/Users/4jp/Workspace/4444J99/estate-vault/sdk/bash/vault-sync.sh`
2. **Estate-wide Adoptions**:
   - Gradually integrate `estate_vault` into other active projects in `/Users/4jp/Workspace/4444J99/` to prevent future disk/git state bloat.

---

## 5. Verification Commands for OpenCode

```bash
# 1. Run Python test suite
pytest

# 2. Check Ruff linter
python3 -m ruff check --select E9,F63,F7,F82 .

# 3. Test live vault sync
python3 scripts/verify_vault_sync.py

# 4. Check git tree status
git status
```
