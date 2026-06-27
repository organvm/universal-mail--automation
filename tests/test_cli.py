"""Focused tests for the top-level CLI orchestration in cli.py."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import cli
from core.audit import AuditLog
from core.models import EmailMessage
from core.rules import add_vip_sender
from providers.base import EmailProvider, ListMessagesResult, ProviderCapabilities


class _FakeProvider(EmailProvider):
    name = "fake"
    capabilities = (
        ProviderCapabilities.TRUE_LABELS
        | ProviderCapabilities.ARCHIVE
        | ProviderCapabilities.STAR
        | ProviderCapabilities.CATEGORIES
    )

    def __init__(
        self,
        messages: list[EmailMessage] | None = None,
        *,
        name: str = "fake",
        capabilities: ProviderCapabilities | None = None,
        total_estimate: int = 3,
        healthy: tuple[bool, str] = (True, "OK"),
    ):
        self.name = name
        if capabilities is not None:
            self.capabilities = capabilities
        self.messages = {msg.id: msg for msg in messages or []}
        self.total_estimate = total_estimate
        self.healthy = healthy
        self.connected = False
        self.disconnected = False
        self.list_calls: list[tuple[str, int, str | None]] = []
        self.labels: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self.archived: list[str] = []
        self.starred: list[tuple[str, object]] = []
        self.categories: list[tuple[str, str, str]] = []

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.disconnected = True

    def health_check(self) -> tuple[bool, str]:
        return self.healthy

    def list_messages(
        self,
        query: str = "",
        limit: int = 100,
        page_token: str | None = None,
    ) -> ListMessagesResult:
        self.list_calls.append((query, limit, page_token))
        if page_token:
            return ListMessagesResult(messages=[], next_page_token=None, total_estimate=0)
        messages = list(self.messages.values())[:limit]
        return ListMessagesResult(
            messages=messages,
            next_page_token=None,
            total_estimate=self.total_estimate,
        )

    def get_message_details(self, message_id: str) -> EmailMessage | None:
        return self.messages.get(message_id)

    def batch_get_details(self, message_ids: list[str]) -> dict[str, EmailMessage]:
        return {msg_id: self.messages[msg_id] for msg_id in message_ids if msg_id in self.messages}

    def apply_label(self, message_id: str, label: str) -> bool:
        self.labels.append((message_id, label))
        return True

    def remove_label(self, message_id: str, label: str) -> bool:
        self.removed.append((message_id, label))
        return True

    def archive(self, message_id: str) -> bool:
        self.archived.append(message_id)
        return True

    def star(self, message_id: str, due_date=None) -> bool:
        self.starred.append((message_id, due_date))
        return True

    def apply_category(self, message_id: str, category: str, color: str = "blue") -> bool:
        self.categories.append((message_id, category, color))
        return True

    def ensure_label_exists(self, label: str) -> str:
        return label


class _ExplodingApplyProvider(_FakeProvider):
    def apply_label(self, message_id: str, label: str) -> bool:  # pragma: no cover
        raise AssertionError("dry-run must not apply labels")

    def remove_label(self, message_id: str, label: str) -> bool:  # pragma: no cover
        raise AssertionError("dry-run must not remove labels")

    def archive(self, message_id: str) -> bool:  # pragma: no cover
        raise AssertionError("dry-run must not archive")


@pytest.fixture(autouse=True)
def _quiet_config(monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda: SimpleNamespace())
    monkeypatch.setattr(cli, "apply_vip_senders_from_config", lambda _config: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)


def _provider_args(**overrides) -> argparse.Namespace:
    data = {
        "provider": "gmail",
        "host": None,
        "user": None,
        "password": None,
        "account": None,
        "gmail_extensions": False,
    }
    data.update(overrides)
    return argparse.Namespace(**data)


def test_get_provider_factory_wires_provider_specific_arguments(monkeypatch):
    made = {}

    class GmailProvider:
        def __init__(self):
            made["gmail"] = True

    class IMAPProvider:
        def __init__(self, host, user, password, use_gmail_extensions):
            made["imap"] = (host, user, password, use_gmail_extensions)

    class MailAppProvider:
        def __init__(self, account):
            made["mailapp"] = account

    class OutlookProvider:
        def __init__(self):
            made["outlook"] = True

    modules = {
        "providers.gmail": ("GmailProvider", GmailProvider),
        "providers.imap": ("IMAPProvider", IMAPProvider),
        "providers.mailapp": ("MailAppProvider", MailAppProvider),
        "providers.outlook": ("OutlookProvider", OutlookProvider),
    }
    for module_name, (class_name, klass) in modules.items():
        module = types.ModuleType(module_name)
        setattr(module, class_name, klass)
        monkeypatch.setitem(sys.modules, module_name, module)

    assert isinstance(cli.get_provider("gmail"), GmailProvider)
    assert isinstance(
        cli.get_provider(
            "imap",
            host="imap.example.test",
            user="u",
            password="p",
            use_gmail_extensions=True,
        ),
        IMAPProvider,
    )
    assert isinstance(cli.get_provider("mailapp", account="iCloud"), MailAppProvider)
    assert isinstance(cli.get_provider("outlook"), OutlookProvider)
    assert made == {
        "gmail": True,
        "imap": ("imap.example.test", "u", "p", True),
        "mailapp": "iCloud",
        "outlook": True,
    }
    with pytest.raises(ValueError, match="Unknown provider"):
        cli.get_provider("not-real")


def test_run_labeler_dry_run_records_protected_hold_and_archive_intent():
    provider = _ExplodingApplyProvider(
        [
            EmailMessage(
                id="p",
                sender="Lawyer <a@example-lawfirm.com>",
                subject="case update",
            ),
            EmailMessage(
                id="n",
                sender="Sale <promo@some-shop.example>",
                subject="50% off newsletter unsubscribe",
            ),
        ]
    )
    audit = AuditLog(provider="fake", dry_run=True)

    result = cli.run_labeler(
        provider=provider,
        query="all",
        limit=10,
        dry_run=True,
        remove_label="Misc/Other",
        state_file=None,
        audit=audit,
    )

    assert result.processed_count == 1
    assert result.success_count == 1
    assert result.error_count == 0
    assert provider.labels == []
    assert provider.archived == []
    assert audit.summary()["protected_held"] == 1
    assert audit.summary()["archived"] == 1


def test_record_dry_run_intent_treats_move_on_label_provider_as_moved():
    provider = _FakeProvider(
        capabilities=ProviderCapabilities.FOLDERS,
    )
    provider.LABEL_IS_MOVE = True
    audit = AuditLog(provider="folder", dry_run=True)

    cli._record_dry_run_intent(
        provider,
        [
            cli.LabelAction(
                message_id="m",
                sender="sender@bulk.example",
                add_labels=["Archive"],
            )
        ],
        audit,
    )

    assert audit.summary()["moved"] == 1
    assert audit.summary()["labeled"] == 0


def test_make_audit_respects_disabled_dry_run_default_and_redaction(tmp_path):
    assert cli._make_audit(argparse.Namespace(no_audit=True, dry_run=False), "triage") is None
    assert cli._make_audit(argparse.Namespace(no_audit=False, dry_run=True), "triage") is None

    audit = cli._make_audit(
        argparse.Namespace(
            no_audit=False,
            dry_run=False,
            provider="imap",
            audit_file=str(tmp_path / "receipt.jsonl"),
            redact_audit=True,
        ),
        "escalate",
    )

    assert audit is not None
    assert audit.path == str(tmp_path / "receipt.jsonl")
    assert audit.provider == "imap"
    assert audit.redact is True


def test_report_audit_surfaces_write_error_and_violation(capsys):
    audit = AuditLog(provider="fake")
    audit.record(
        message_id="protected",
        sender="alerts@chase.com",
        protected=False,
        archived=True,
    )
    audit.write_error = "disk full"

    assert cli._report_audit(audit) is True

    out = capsys.readouterr().out
    assert "Triage receipt:" in out
    assert "could NOT be written" in out
    assert "disk full" in out


def test_cmd_label_applies_actions_and_prints_statistics(monkeypatch, capsys):
    provider = _FakeProvider(
        [
            EmailMessage(
                id="n",
                sender="Sale <promo@some-shop.example>",
                subject="50% off newsletter unsubscribe",
            )
        ]
    )
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    rc = cli.cmd_label(
        _provider_args(
            query="all",
            limit=1,
            dry_run=False,
            remove_label="Uncategorized",
            state_file=None,
            tier_routing=False,
            vip_only=False,
            no_audit=True,
            audit_file=None,
            redact_audit=False,
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert provider.connected is True
    assert provider.disconnected is True
    assert provider.labels
    assert ("n", "Uncategorized") in provider.removed
    assert provider.archived == ["n"]
    assert "PROCESSING STATISTICS" in out
    assert "Total Processed: 1" in out


def test_cmd_report_counts_gmail_labels(monkeypatch, capsys):
    provider = _FakeProvider(name="gmail", total_estimate=42)
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    rc = cli.cmd_report(_provider_args(provider="gmail"))

    out = capsys.readouterr().out
    assert rc == 0
    assert "# Email Report - gmail" in out
    assert "- Dev/GitHub: 42" in out
    assert any(call[0] == "label:Dev/GitHub" and call[1] == 1 for call in provider.list_calls)


def test_cmd_health_reports_success_and_connection_failure(monkeypatch, capsys):
    provider = _FakeProvider(name="imap", healthy=(True, "ready"))
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    assert cli.cmd_health(_provider_args(provider="imap")) == 0
    assert "imap: ready" in capsys.readouterr().out
    assert provider.disconnected is True

    class BrokenProvider(_FakeProvider):
        def connect(self) -> None:
            raise RuntimeError("no credentials")

    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: BrokenProvider(name="imap"))

    assert cli.cmd_health(_provider_args(provider="imap")) == 1
    assert "Connection failed" in capsys.readouterr().out


def test_cmd_summary_json_counts_priority_tiers(monkeypatch, capsys):
    provider = _FakeProvider(
        [
            EmailMessage(
                id="g",
                sender="notifications@github.com",
                subject="[repo] pull request opened",
            ),
            EmailMessage(
                id="m",
                sender="Sale <promo@some-shop.example>",
                subject="weekly newsletter",
            ),
        ],
        name="gmail",
    )
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    rc = cli.cmd_summary(
        _provider_args(provider="gmail", query="in:anywhere", limit=10, format="json")
    )

    body = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert body["provider"] == "gmail"
    assert body["total"] == 2
    assert body["tiers"]["2"]["count"] == 1
    assert body["tiers"]["4"]["count"] == 1
    assert provider.list_calls == [("in:anywhere", 10, None)]


def test_cmd_pending_json_filters_starred_messages(monkeypatch, capsys):
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    provider = _FakeProvider(
        [
            EmailMessage(
                id="starred",
                sender="notifications@github.com",
                subject="review requested",
                date=old,
                is_starred=True,
            ),
            EmailMessage(
                id="plain",
                sender="plain@example.test",
                subject="FYI",
                date=old,
                is_starred=False,
            ),
        ]
    )
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    rc = cli.cmd_pending(_provider_args(provider="gmail", limit=5, format="json"))

    body = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [item["id"] for item in body] == ["starred"]
    assert body[0]["tier"] == 2
    assert provider.list_calls == [("is:starred", 5, None)]


def test_cmd_vip_markdown_reports_configured_vip_activity(monkeypatch, capsys):
    add_vip_sender(
        key="ceo",
        pattern=r"ceo@example\.com",
        tier=1,
        star=True,
        note="Board sponsor",
    )
    provider = _FakeProvider(
        [
            EmailMessage(
                id="vip",
                sender="CEO <ceo@example.com>",
                subject="Need your answer",
                date=datetime(2026, 6, 1, tzinfo=timezone.utc),
            )
        ]
    )
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    rc = cli.cmd_vip(_provider_args(provider="gmail", query="recent", limit=10, format="markdown"))

    out = capsys.readouterr().out
    assert rc == 0
    assert "# VIP Sender Activity - fake" in out
    assert "## Board sponsor (Tier 1)" in out
    assert "Messages: 1" in out


def test_cmd_escalate_dry_run_reports_stale_message_without_applying(monkeypatch, capsys):
    stale = datetime.now(timezone.utc) - timedelta(days=5)
    provider = _FakeProvider(
        [
            EmailMessage(
                id="old",
                sender="updates@newsletter.example",
                subject="weekly newsletter",
                date=stale,
            )
        ]
    )
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    rc = cli.cmd_escalate(
        _provider_args(
            provider="gmail",
            query="older",
            limit=5,
            dry_run=True,
            no_audit=False,
            audit_file=None,
            redact_audit=False,
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Messages checked: 1" in out
    assert "Messages escalated: 1" in out
    assert provider.labels == []
    assert provider.categories == []


def test_cmd_triage_limits_to_top_and_reassigns_rank(monkeypatch, capsys, tmp_path):
    provider = _FakeProvider(
        [
            EmailMessage(
                id="a",
                sender="sender@example.test",
                subject="Can you review this?",
            ),
            EmailMessage(
                id="b",
                sender="other@example.test",
                subject="FYI",
            ),
        ]
    )
    monkeypatch.setattr(cli, "get_provider", lambda *args, **kwargs: provider)

    import core.triage as triage_module
    import core.voice as voice_module

    items = [SimpleNamespace(rank=99, message_id="a"), SimpleNamespace(rank=100, message_id="b")]
    calls = {}

    def fake_load_voice_profile(path=None, samples_path=None, name=""):
        calls["voice"] = (path, samples_path, name)
        return "voice"

    def fake_triage_messages(messages, voice=None, draft=False):
        calls["triage"] = ([msg.id for msg in messages], voice, draft)
        return items

    def fake_render_triage(render_items, fmt):
        calls["render"] = ([item.rank for item in render_items], fmt)
        return "rendered triage"

    monkeypatch.setattr(voice_module, "load_voice_profile", fake_load_voice_profile)
    monkeypatch.setattr(triage_module, "triage_messages", fake_triage_messages)
    monkeypatch.setattr(triage_module, "render_triage", fake_render_triage)

    rc = cli.cmd_triage(
        _provider_args(
            provider="gmail",
            query="triage",
            limit=10,
            top=1,
            format="markdown",
            draft=True,
            voice_file=str(tmp_path / "voice.json"),
            samples_file=str(tmp_path / "samples.txt"),
            name="Anthony",
        )
    )

    assert rc == 0
    assert capsys.readouterr().out.strip() == "rendered triage"
    assert calls["triage"] == (["a", "b"], "voice", True)
    assert calls["render"] == ([1], "markdown")
    assert calls["voice"][2] == "Anthony"


def test_main_dispatches_verbose_subcommand(monkeypatch):
    seen = {}

    def fake_health(args):
        seen["args"] = args
        return 7

    monkeypatch.setattr(cli, "cmd_health", fake_health)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cli.py", "--verbose", "health", "--provider", "imap", "--host", "imap.example.test"],
    )

    assert cli.main() == 7
    assert seen["args"].provider == "imap"
    assert seen["args"].host == "imap.example.test"
    assert logging.getLogger().level == logging.DEBUG

