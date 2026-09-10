"""Operator-owned send boundary; no CLI flag or historic receipt overrides it."""
import json
from pathlib import Path


POLICY_PATH = Path(__file__).resolve().parents[1] / "config" / "mail-execution-policy.json"


def automated_send_allowed() -> bool:
    try:
        policy = json.loads(POLICY_PATH.read_text())
    except (OSError, ValueError):
        return False
    return (policy.get("schema") == "uma.mail_execution_policy.v1"
            and policy.get("send_authority") == "explicit_programmatic_receipt"
            and policy.get("automated_send_allowed") is True)
