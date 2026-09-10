import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from core.config import (
    find_synth_config_file,
    load_synth_config,
    load_and_apply_synth_config,
    create_sample_synth_config,
    apply_identity_from_config,
    apply_patchbay_from_config,
)
from core.identity import DEFAULT_DIRECTORY
from core.patchbay import DEFAULT_PATCHBAY, PatchCable, SinkType
from core.models import CommAction, CommMessage


def test_create_sample_synth_config(tmp_path):
    sample_file = tmp_path / "sample_synth.yaml"
    content = create_sample_synth_config(sample_file)
    assert "version: \"1.0\"" in content
    assert "entities:" in content
    assert "patch_cables:" in content
    assert sample_file.exists()
    assert sample_file.read_text() == content


def test_find_synth_config_file_env_override(tmp_path):
    custom_config = tmp_path / "my_synth.yaml"
    custom_config.write_text("version: '1.0'\n")

    with patch.dict(os.environ, {"SYNTH_CONFIG": str(custom_config)}):
        found = find_synth_config_file()
        assert found == custom_config


def test_load_and_apply_synth_config(tmp_path):
    synth_file = tmp_path / "synth.yaml"
    synth_file.write_text("""
version: "1.0"
entities:
  partner:
    name: "Strategic Partner"
    emails: ["partner@synth.test"]
    phones: ["+15555559999"]
    is_vip: true
    is_protected: true

patch_cables:
  - name: "synth-webhook"
    sink_type: "webhook"
    destination: "http://localhost:9999/hook"
    min_tier: 1
""")

    result = load_and_apply_synth_config(synth_file)
    assert result["entities"] >= 1
    assert result["patch_cables"] >= 1

    # Verify identity registered
    ent = DEFAULT_DIRECTORY.resolve("partner@synth.test")
    assert ent is not None
    assert ent.name == "Strategic Partner"
    assert ent.is_vip is True
    assert ent.is_protected is True

    # Verify patch cable registered
    active = DEFAULT_PATCHBAY.active_cables()
    cable_names = [c.name for c in active]
    assert "synth-webhook" in cable_names
    DEFAULT_PATCHBAY.disconnect("synth-webhook")


def test_local_log_ledger_sink(tmp_path):
    ledger_file = tmp_path / "audit" / "synth_ledger.jsonl"
    cable = PatchCable(
        name="test-ledger",
        sink_type=SinkType.LOCAL_LOG,
        destination=str(ledger_file),
    )
    DEFAULT_PATCHBAY.connect(cable)

    action = CommAction(
        message_id="msg-ledger-1",
        channel_id="email",
        sender="client@example.com",
        archive=True,
        star=False,
    )
    msg = CommMessage(
        id="msg-ledger-1",
        channel_id="email",
        sender="client@example.com",
        subject="Invoice inquiry",
        priority_tier=3,
    )

    try:
        receipts = DEFAULT_PATCHBAY.transmit(action, msg)
        assert len(receipts) == 1
        assert receipts[0]["status"] == "ledger_written"

        assert ledger_file.exists()
        lines = ledger_file.read_text().strip().split("\n")
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["cable"] == "test-ledger"
        assert record["message_id"] == "msg-ledger-1"
        assert record["sender"] == "client@example.com"
        assert record["priority_tier"] == 3
        assert record["archive"] is True
    finally:
        DEFAULT_PATCHBAY.disconnect("test-ledger")
