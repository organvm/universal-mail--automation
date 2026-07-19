"""Static guard tests for literal UMA mail-triage ownership."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check-mail-union.py"


def test_mail_union_guard_allows_wrappers_and_blocks_classifiers(tmp_path):
    uma_root = tmp_path / "uma"
    other_root = tmp_path / "other"
    uma_root.mkdir()
    other_root.mkdir()
    (uma_root / "owned.py").write_text(
        "SPAM_SENDER_PATTERNS = []\ndef classify_message(msg):\n    return 'HUMAN'\n",
        encoding="utf-8",
    )
    (other_root / "wrapper.sh").write_text(
        "# UMA_MAIL_TRIAGE_WRAPPER\nexec umail mail-status \"$@\"\n",
        encoding="utf-8",
    )
    (other_root / "mail-triage.py").write_text(
        "SPAM_SENDER_PATTERNS = []\ndef classify_message(msg):\n    return 'HUMAN'\n",
        encoding="utf-8",
    )

    failed = subprocess.run(
        [
            sys.executable,
            str(GUARD),
            "--uma-root",
            str(uma_root),
            "--root",
            str(other_root),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert failed.returncode == 1
    assert "mail_triage_classifier_outside_uma" in failed.stdout
    assert "wrapper.sh" not in failed.stdout

    (other_root / "mail-triage.py").write_text(
        "# UMA_MAIL_TRIAGE_WRAPPER\nexec umail mail-status\n",
        encoding="utf-8",
    )
    passed = subprocess.run(
        [
            sys.executable,
            str(GUARD),
            "--uma-root",
            str(uma_root),
            "--root",
            str(other_root),
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert '"status": "ok"' in passed.stdout
