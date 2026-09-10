from types import SimpleNamespace
import json

import pytest

from core.obligation_research import research
from core.obligation_refresh import refresh, shadow_from_observation
from providers.mailapp import MailAppProvider, ProviderWriteAmbiguous
from core.models import FlagColor
from tests.test_mailapp import _ScopedHarness as H


def test_delayed_sync_reads_twice_without_repeat_write(monkeypatch):
    sleeps = []
    monkeypatch.setattr("providers.mailapp.time.sleep", sleeps.append)
    matched = H.RESOLVED.replace("0\x1f", "1\x1f", 1)
    provider = H.prov([H.RESOLVED, "ok", H.RESOLVED, H.RESOLVED, matched])
    assert provider.set_flag_color_ref(H.make_ref(), FlagColor.ORANGE)
    assert sleeps == [2, 3]
    assert sum("set flag index of targetMessage" in c for c in provider.calls) == 1


def test_native_color_without_flagged_status_is_pending(monkeypatch):
    monkeypatch.setattr("providers.mailapp.time.sleep", lambda _: None)
    wrong = H.RESOLVED.replace("0\x1f", "1\x1f", 1).rsplit("\x1f", 1)[0] + "\x1ffalse"
    provider = H.prov([H.RESOLVED, "ok", wrong, wrong, wrong])
    with pytest.raises(ProviderWriteAmbiguous, match="pending"):
        provider.set_flag_color_ref(H.make_ref(), FlagColor.ORANGE)
    assert sum("set flag index of targetMessage" in c for c in provider.calls) == 1


def test_early_match_followed_by_reversion_is_not_verified(monkeypatch):
    sleeps = []
    monkeypatch.setattr("providers.mailapp.time.sleep", sleeps.append)
    matched = H.RESOLVED.replace("0\x1f", "1\x1f", 1)
    provider = H.prov([H.RESOLVED, "ok", matched, matched, H.RESOLVED])
    with pytest.raises(ProviderWriteAmbiguous, match="pending"):
        provider.set_flag_color_ref(H.make_ref(), FlagColor.ORANGE)
    assert sleeps == [2, 3]


def test_research_resume_advances_and_rejects_changed_source(tmp_path):
    class Provider:
        def read_evidence_ref(self, ref):
            return {"headers": f"Message-ID: <{ref.provider_id}@example.invalid>"}
    ref = H.make_ref().__dict__
    observation = {"surfaces": [], "messages": [dict(reference={**ref, "provider_id": str(i)},
                   native_index=5, provider_id=str(i)) for i in range(30)]}
    output = tmp_path / "research.json"
    first = research(Provider(), observation, output=output)
    second = research(Provider(), observation, output=output, resume=first)
    assert len(second["threads"]) == 30
    assert second["next_candidate"] == 30
    assert second["unattempted"] == []
    assert len({t["seed"]["provider_id"] for t in second["threads"]}) == 30
    with pytest.raises(ValueError, match="lineage"):
        research(Provider(), {**observation, "changed": True}, output=output, resume=first)


def test_rebind_requires_exact_rfc_and_envelope():
    provider = H.prov(["43", H.RESOLVED])
    live = provider.refresh_reference(H.make_ref(), "Message-ID: <abc@x>")
    assert live.provider_id == "43"
    assert all("set flag index" not in c for c in provider.calls)
    assert "count of candidates) is not 1" in provider.calls[0]


def test_rebind_wrong_evidence_never_reads():
    provider = H.prov([])
    with pytest.raises(RuntimeError, match="exact RFC"):
        provider.refresh_reference(H.make_ref(), "Message-ID: <unrelated@x>")
    assert provider.calls == []


def test_stable_host_and_no_window_activation(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr("providers.mailapp.subprocess.run", run)
    monkeypatch.setattr("providers.mailapp.sys.platform", "darwin")
    MailAppProvider().connect()
    command, kwargs = calls[0]
    assert command[0].endswith("/.local/bin/domus-agent-host")
    assert command[1:4] == ["ensure", "--", "/usr/bin/osascript"]
    assert "activate" not in command[-1]
    assert kwargs["timeout"] <= 30


def test_inbox_refresh_includes_unflagged_predicate():
    script = MailAppProvider()._build_flagged_script("INBOX", "a", None, flagged_only=False)
    assert "whose flagged status" not in script
    assert "messages of targetMailbox" in script


def test_research_hard_bounds_and_durable_failures(tmp_path):
    class Provider:
        def read_evidence_ref(self, ref):
            raise RuntimeError("offline")
    ref = H.make_ref().__dict__
    observation = {"surfaces": [], "messages": [dict(reference={**ref, "provider_id": str(i+1)},
                   native_index=5, provider_id=str(i+1)) for i in range(30)]}
    output = tmp_path / "private" / "research.json"
    result = research(Provider(), observation, output=output)
    assert len(result["threads"]) == 25
    assert len(result["unattempted"]) == 5
    assert result["writes_performed"] == 0
    assert json.loads(output.read_text()) == result
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        research(Provider(), observation, output=output, thread_limit=26)


def test_failed_discovery_remains_coverage_gap(tmp_path):
    class Provider:
        def discover_surfaces(self, **kwargs):
            raise RuntimeError("offline")
    result = refresh(Provider(), accounts=["a"], output=tmp_path / "private" / "obs.json")
    assert result["coverage_gaps"] == ["surface_discovery_unavailable"]
    assert shadow_from_observation(result)["metrics"]["coverage_gaps"] == 1


def test_storage_failure_prevents_further_observation(monkeypatch, tmp_path):
    def fail(*a, **k):
        raise OSError("full disk")
    monkeypatch.setattr("core.obligation_refresh._atomic_write_private_json", fail)
    with pytest.raises(OSError):
        refresh(object(), accounts=["a"], output=tmp_path / "obs.json")
