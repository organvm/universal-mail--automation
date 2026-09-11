# OpenCode Instructions & Context Capsule

Welcome to `universal-mail--automation` (UMA) and the wider `4444J99` estate.

> **Before any work, read the formal handoff:** 👉 **[`.conductor/active-handoff.md`](.conductor/active-handoff.md)**
> It carries trunk state, locked constraints, estate quota status, and next missions. Do not violate §2 Hard Constraints.

## Quick Start (zero-friction launcher)

```bash
# One-command handoff boot (validates tree, runs ruff + vault probe, then execs opencode)
./scripts/handoff_to_opencode.sh

# CI / dry-run (checks only, no exec)
./scripts/handoff_to_opencode.sh --dry-run

# Manual fallback
/opt/homebrew/bin/opencode  # then read .conductor/active-handoff.md first
```

## Architecture Quick Reference

| Area | Key files |
|------|-----------|
| **Engine Core** | `cli.py`, `core/rules.py` (LABEL_RULES/tiering), `core/vault_sync.py`, `core/state.py`, `core/patchbay.py`, `core/models.py` |
| **Providers** | `providers/gmail.py`, `providers/imap.py`, `providers/outlook.py`, `providers/mailapp.py` + `*.applescript` (Apple Mail bridge) |
| **State (decoupled)** | `core/vault_sync.py` + `core/state.py` → GitHub API → `4444J99/estate-vault/universal-mail/labeler_state.json`. SDK: `../estate-vault/sdk/python/estate_vault.py`, `../estate-vault/sdk/bash/vault-sync.sh`. Never write `labeler_state.json`/`mail_export.tsv`/`*_state.json` to git. |
| **Web** | `web/` Next.js 16 dashboard (`web/src/app/page.tsx` — must fallback to API/Vault, not `../labeler_state.json`) |
| **Governance** | `BRANCHES.md` (standing lanes `main`/`lane/verify`/`lane/heal`/`lane/expand-providers`/`lane/evolve`), `docs/policies/email-envelope-doctrine.md` (30–90w target, 120 soft / 150 hard), secrets via `~/.config/op/*.env.op.sh` (1Password, `VAULT_PAT`/`VAULT_REPO`) |

## Test & Validation Commands

```bash
pytest                                          # 1670 passed, 4 skipped expected
python3 -m ruff check --select E9,F63,F7,F82 .  # 0 errors
python3 scripts/verify_vault_sync.py            # live GH API read-after-write vs estate-vault
python3 -m build --no-isolation                 # package build
git status                                      # must be clean (handoff committed)
```

Full suites: `python3 -m pytest tests/test_research.py tests/test_voice.py tests/test_triage.py -q`, `python3 -m pytest tests/test_ops.py -q`.

## Immediate Priorities (from handoff §4)

1. **Provider expansion** (`lane/expand-providers`): `providers/imap.py` + `providers/outlook.py` edge cases, validate `*.applescript`.
2. **Estate backup** (`4444J99/estate-vault/sdk`): backup routines via `estate_vault.py`/`vault-sync.sh`.
3. **Web decoupling** (`web/`): render without local state dep (API/Vault fallback).
4. Always: keep trunk honest (no unmerged lanes, no broken tests), keep state in vault.

## Constraints Recap (handoff §2)

- Decoupled state only; `BRANCHES.md` lane discipline; envelope doctrine `docs/policies/email-envelope-doctrine.md`; zero credentials in repo.
- If handoff says `CROSS-VERIFICATION REQUIRED`, self-assessment not trusted — await separate verifier.
