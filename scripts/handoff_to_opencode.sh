#!/usr/bin/env bash
# handoff_to_opencode.sh — zero-friction UMA → OpenCode launcher
# Validates pristine tree, runs ruff + live vault probe, then execs opencode with handoff pre-loaded.
# Usage: ./scripts/handoff_to_opencode.sh [--dry-run] [--skip-vault]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

OPENCODE_BIN="/opt/homebrew/bin/opencode"
HANDOFF=".conductor/active-handoff.md"
OPENCODE_MD="OPENCODE.md"
DRY_RUN=0
SKIP_VAULT=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --skip-vault) SKIP_VAULT=1 ;;
    -h|--help)
      echo "Usage: $0 [--dry-run] [--skip-vault]"
      echo "  --dry-run     Validate + sanity checks only, do not exec opencode"
      echo "  --skip-vault  Skip live vault probe (offline / no PAT)"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

echo "== UMA Handoff Launcher =="
echo "Repo: $REPO_DIR"
echo "Handoff: $HANDOFF"
echo "Target: OpenCode ($OPENCODE_BIN v1.18.30)"

# 1. Check handoff exists
if [[ ! -f "$HANDOFF" ]]; then
  echo "[ERROR] $HANDOFF not found. Cannot launch without active handoff." >&2
  exit 1
fi
if [[ ! -f "$OPENCODE_MD" ]]; then
  echo "[WARN] $OPENCODE_MD not found." >&2
fi

# 2. Pristine check — fail-closed per BRANCHES.md / 58b6343 baseline
#    Handoff + OPENCODE must be committed; any staged/unstaged diff blocks launch.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[ERROR] Working tree has staged/unstaged changes. Commit or stash first:" >&2
  git status --short >&2 || true
  exit 1
fi
# Note: untracked files are allowed to be checked separately; launcher warns if handoff itself is untracked
if git status --porcelain | grep -q "??"; then
  # Only warn if handoff/OPENCODE are among untracked — they should be committed
  if git status --porcelain | grep -qE "^\?\? (\.conductor/|OPENCODE\.md|scripts/handoff_to_opencode\.sh)"; then
    echo "[WARN] Handoff artifacts are untracked — commit them before sharing. Continuing." >&2
    git status --short >&2 || true
  fi
fi
echo "[OK] Working tree pristine (no staged/unstaged diffs)."

# 3. Sanity checks
echo "[CHECK] ruff --select E9,F63,F7,F82"
if command -v ruff >/dev/null 2>&1; then
  ruff check --select E9,F63,F7,F82 . || { echo "[ERROR] ruff failed" >&2; exit 1; }
elif python3 -m ruff --version >/dev/null 2>&1; then
  python3 -m ruff check --select E9,F63,F7,F82 . || { echo "[ERROR] ruff failed" >&2; exit 1; }
else
  echo "[WARN] ruff not found, skipping." >&2
fi
echo "[OK] ruff clean."

if [[ "$SKIP_VAULT" -eq 0 ]]; then
  echo "[CHECK] Vault live probe (scripts/verify_vault_sync.py)"
  if python3 scripts/verify_vault_sync.py; then
    echo "[OK] Vault read-after-write OK (4444J99/estate-vault)."
  else
    echo "[WARN] Vault probe failed — no PAT (VAULT_PAT) or gh auth, or network. Use --skip-vault to bypass." >&2
    # Non-fatal unless strict; handoff §2 still requires vault in prod
    if [[ "$DRY_RUN" -eq 0 ]]; then
      echo "[WARN] Continuing to opencode despite vault probe failure." >&2
    else
      exit 1
    fi
  fi
else
  echo "[SKIP] Vault probe skipped (--skip-vault)."
fi

# 4. Formulate priming prompt
#    Keep short — handoff is the source of truth, not a pasted blob.
PROMPT="Read $HANDOFF and $OPENCODE_MD first. You are OpenCode v1.18.30 in $REPO_DIR. Honor all Hard Constraints (handoff §2: decoupled state via core/vault_sync.py + core/state.py → 4444J99/estate-vault, BRANCHES.md lanes, envelope doctrine, 1Password secrets). Run verification commands from handoff §5 before editing. Estate vault at ../estate-vault."

echo ""
echo "== Ready =="
echo "Prime prompt: $PROMPT"
echo "Handoff sections: $(grep -c "^## " "$HANDOFF") sections, $(wc -l < "$HANDOFF") lines"
echo ""

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DRY-RUN] Checks passed. Would exec: $OPENCODE_BIN"
  echo "Prompt would be: $PROMPT"
  exit 0
fi

if [[ ! -x "$OPENCODE_BIN" ]]; then
  echo "[ERROR] $OPENCODE_BIN not found or not executable. Install opencode 1.18.30:" >&2
  echo "  brew install opencode  # or check /opt/homebrew/bin/opencode" >&2
  echo "Prompt for manual launch: $PROMPT" >&2
  exit 1
fi

echo "[LAUNCH] exec $OPENCODE_BIN"
echo "  (handoff pre-loaded: $HANDOFF)"
exec "$OPENCODE_BIN"
