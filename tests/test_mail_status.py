"""Unified UMA mail status and terminal crosswalk tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from core.mail_status import (
    MAIL_HISTORICAL_CROSSWALK_SCHEMA,
    MAIL_STATUS_SCHEMA,
    PROCESSING_STATES,
    TERMINAL_STATUSES,
    build_historical_crosswalk,
    build_mail_status,
)

ROOT = Path(__file__).resolve().parents[1]
HISTORICAL_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "historical"
OPS_FIXTURE_DIR = ROOT / "tests" / "fixtures" / "ops"


def _write_history(tmp_path: Path) -> Path:
    target = tmp_path / "history.json"
    shutil.copyfile(HISTORICAL_FIXTURE_DIR / "latest.json", target)
    return target


def _write_ops_report(tmp_path: Path) -> Path:
    target = tmp_path / "latest.json"
    shutil.copyfile(OPS_FIXTURE_DIR / "latest.json", target)
    shutil.copyfile(OPS_FIXTURE_DIR / "latest-actions.md", tmp_path / "latest-actions.md")
    return target


def test_historical_crosswalk_reconciles_every_source_message_and_stays_redacted(tmp_path):
    history = _write_history(tmp_path)
    ops_report = _write_ops_report(tmp_path)

    crosswalk = build_historical_crosswalk(history, ops_report_path=ops_report)
    payload = json.dumps(crosswalk, sort_keys=True)

    assert crosswalk["schema"] == MAIL_HISTORICAL_CROSSWALK_SCHEMA
    assert crosswalk["mode"]["read_only"] is True
    assert crosswalk["mode"]["mailbox_mutations"] is False
    assert crosswalk["mode"]["sends"] is False
    assert crosswalk["mode"]["deletes"] is False
    assert crosswalk["mode"]["credential_changes"] is False
    assert crosswalk["terminal_statuses"] == list(TERMINAL_STATUSES)
    assert crosswalk["processing_states"] == list(PROCESSING_STATES)
    assert crosswalk["kpis"]["source_messages"] == 5
    assert crosswalk["kpis"]["terminal_status_total"] == 5
    assert crosswalk["kpis"]["explicit_exclusions"] == 0
    assert crosswalk["kpis"]["reconciled"] is True
    assert sum(crosswalk["kpis"]["terminal_status_counts"].values()) == 5
    assert crosswalk["kpis"]["represented_in_ops"] >= 1
    assert "blocked" in crosswalk["kpis"]["terminal_status_counts"]
    assert "Private Recruiter" not in payload
    assert "private-recruiter@example.test" not in payload
    assert "Private lead should not leak" not in payload
    assert "Could you send your availability" not in payload


def test_mail_status_combines_ops_history_ledgers_and_next_queue(tmp_path):
    history = _write_history(tmp_path)
    ops_report = _write_ops_report(tmp_path)
    action_ledger = tmp_path / "mail-action-ledger.jsonl"
    resolver_ledger = tmp_path / "mail-resolver-ledger.jsonl"
    draft_approvals = tmp_path / "mail-draft-approvals.jsonl"
    delivery_ledger = tmp_path / "mail-delivery-ledger.jsonl"

    status = build_mail_status(
        ops_report_path=ops_report,
        history_path=history,
        action_ledger_path=action_ledger,
        resolver_ledger_path=resolver_ledger,
        draft_approval_path=draft_approvals,
        delivery_path=delivery_ledger,
        max_age_hours=24 * 365,
    )
    payload = json.dumps(status, sort_keys=True)

    assert status["schema"] == MAIL_STATUS_SCHEMA
    assert status["mode"]["apply_means_real_mailbox_mutation"] is True
    assert status["mode"]["mailbox_mutations"] is False
    assert status["mode"]["sends"] is False
    assert status["current_ops"]["available"] is True
    assert status["historical_crosswalk"]["available"] is True
    assert status["historical_crosswalk"]["kpis"]["source_messages"] == 5
    assert status["answers"]["what_mailbox_surface_was_covered"]["historical_messages"] == 5
    assert status["answers"]["what_changed"] == {
        "mailbox_mutations": 0,
        "sends": 0,
        "deletes": 0,
        "credential_changes": 0,
    }
    assert status["answers"]["is_historical_backlog_terminally_accounted_for"] is True
    assert isinstance(status["next_queue"], list)
    assert "Private Person" not in payload
    assert "Private Billing" not in payload
    assert "Synthetic body" not in payload


def test_mail_status_cli_and_crosswalk_cli_smoke(tmp_path):
    history = _write_history(tmp_path)
    ops_report = _write_ops_report(tmp_path)

    crosswalk_proc = subprocess.run(
        [
            sys.executable,
            "cli.py",
            "mail-historical-crosswalk",
            "--history",
            str(history),
            "--ops-report",
            str(ops_report),
            "--require-reconciled",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    crosswalk = json.loads(crosswalk_proc.stdout)
    assert crosswalk["schema"] == MAIL_HISTORICAL_CROSSWALK_SCHEMA
    assert crosswalk["kpis"]["reconciled"] is True
    assert "Private Recruiter" not in crosswalk_proc.stdout

    status_proc = subprocess.run(
        [
            sys.executable,
            "cli.py",
            "mail-status",
            "--history",
            str(history),
            "--ops-report",
            str(ops_report),
            "--max-age-hours",
            str(24 * 365),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    status = json.loads(status_proc.stdout)
    assert status["schema"] == MAIL_STATUS_SCHEMA
    assert status["historical_crosswalk"]["kpis"]["source_messages"] == 5
    assert "private-recruiter@example.test" not in status_proc.stdout
