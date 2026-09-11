#!/usr/bin/env python3
"""
Diagnostic script to verify connectivity and bidirectional sync
between universal-mail--automation and 4444J99/estate-vault.
"""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

# Add repo root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.vault_sync import VaultSync

def get_token():
    token = os.environ.get("VAULT_PAT")  # allow-secret
    if token:
        return token
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()  # allow-secret
        if token:
            return token
    except Exception:
        pass
    return None

def main():
    repo = os.environ.get("VAULT_REPO", "4444J99/estate-vault")
    path = os.environ.get("VAULT_PATH", "universal-mail/labeler_state.json")
    
    token = get_token()  # allow-secret
    if not token:
        print("[ERROR] No GitHub token found. Set VAULT_PAT or log in with `gh auth login`.", file=sys.stderr)
        sys.exit(1)
        
    print(f"[INFO] Connecting to Vault: {repo} (Path: {path})")
    vault = VaultSync(repo=repo, pat=token, path=path)
    
    print("[INFO] Probing remote state...")
    existing = vault.pull()
    if existing:
        print(f"[SUCCESS] Retrieved existing state: total_processed={existing.get('total_processed', 0)}, last_run={existing.get('last_run')}")
    else:
        print("[INFO] No previous state file found in vault (or 404).")
        
    checkpoint_data = existing or {
        "next_page_token": None,
        "total_processed": 0,
        "history": {},
        "provider": "vault_verifier"
    }
    checkpoint_data["last_verified"] = datetime.now().isoformat()
    checkpoint_data["verifier_status"] = "OK"
    
    print("[INFO] Testing push/commit to Vault...")
    success = vault.push(checkpoint_data, commit_message=f"verify: connection probe {datetime.now().isoformat()}")
    if success:
        print("[SUCCESS] Successfully committed state checkpoint to estate-vault!")
        
        # Verify read-after-write
        verify_pull = vault.pull()
        if verify_pull and verify_pull.get("verifier_status") == "OK":
            print("[SUCCESS] Read-after-write verification passed perfectly.")
            return 0
        else:
            print("[ERROR] Verification read failed or mismatched content.", file=sys.stderr)
            return 1
    else:
        print("[ERROR] Failed to push state checkpoint to vault.", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
