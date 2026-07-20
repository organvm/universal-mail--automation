"""Focused tests for the credential-neutral VOX transport adapter."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import pytest

from core.vox_transport import (
    AuthenticatedInbound,
    HttpResponse,
    VoxClient,
    VoxConfigurationError,
    VoxRenderProfile,
    VoxTransportAdapter,
    VoxTransportError,
    build_vox_adapter_from_env,
    load_vox_render_profile,
)


class FakeHttpTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers, body, timeout):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return self.responses.pop(0)


def _json_response(value, status=200):
    return HttpResponse(
        status=status,
        body=json.dumps(value).encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def _form(request):
    return parse_qs(request["body"].decode("utf-8"), keep_blank_values=True)


def test_email_adapter_posts_only_phase_3a_fields():
    http = FakeHttpTransport(
        [
            _json_response(
                {"message_id": "message-1", "job_id": "job-1", "source": "email"}
            )
        ]
    )
    adapter = VoxTransportAdapter(VoxClient("https://vox.example.test", transport=http))
    message = AuthenticatedInbound.email(
        sender="sender@example.test",
        subject="A subject",
        body="Message text",
        authentication_receipt="gmail-history:123",
    )

    receipt = adapter.forward(message)

    assert receipt.render_status == "pending"
    assert receipt.audio_routed is False
    assert http.requests[0]["url"] == "https://vox.example.test/ingest/email"
    assert _form(http.requests[0]) == {
        "sender": ["sender@example.test"],
        "subject": ["A subject"],
        "body": ["Message text"],
    }
    assert "authentication_receipt" not in http.requests[0]["body"].decode("utf-8")
    assert "sender@example.test" not in repr(receipt)


def test_sms_adapter_uses_twilio_contract_after_authentication():
    http = FakeHttpTransport(
        [
            _json_response(
                {"message_id": "message-2", "job_id": "job-2", "source": "sms"}
            )
        ]
    )
    adapter = VoxTransportAdapter(VoxClient("http://vox.internal", transport=http))

    adapter.forward(
        AuthenticatedInbound.sms(
            sender="+15550001111",
            body="Read this back",
            authentication_receipt="twilio-signature:verified",
        )
    )

    assert http.requests[0]["url"] == "http://vox.internal/ingest/sms"
    assert _form(http.requests[0]) == {
        "From": ["+15550001111"],
        "Body": ["Read this back"],
    }


def test_missing_authentication_receipt_fails_before_network():
    http = FakeHttpTransport([])
    adapter = VoxTransportAdapter(VoxClient("https://vox.example.test", transport=http))

    with pytest.raises(VoxTransportError, match="authenticated transport receipt"):
        adapter.forward(
            AuthenticatedInbound.email(
                sender="sender@example.test",
                subject="subject",
                body="body",
                authentication_receipt="",
            )
        )

    assert http.requests == []


def test_audio_sink_without_render_profile_fails_before_network():
    http = FakeHttpTransport([])
    adapter = VoxTransportAdapter(VoxClient("https://vox.example.test", transport=http))

    with pytest.raises(VoxConfigurationError, match="audio routing requires"):
        adapter.forward(
            AuthenticatedInbound.sms(
                sender="+15550001111",
                body="body",
                authentication_receipt="verified-event",
            ),
            audio_sink=lambda *_: None,
        )

    assert http.requests == []


def test_render_and_audio_route_are_runtime_selected_and_receipt_is_redacted():
    http = FakeHttpTransport(
        [
            _json_response({"message_id": "message-3", "job_id": "job-3"}),
            _json_response({"id": "job-3", "status": "done"}),
            HttpResponse(
                status=200,
                body=b"synthetic-audio",
                headers={"Content-Type": "audio/wav"},
            ),
        ]
    )
    adapter = VoxTransportAdapter(
        VoxClient(
            "https://vox.example.test", service_token="service-secret", transport=http
        )
    )
    profile = VoxRenderProfile(
        voice_id="runtime-voice-id",
        style_key="future-style",
        provider="runtime-provider",
    )
    routed = []

    receipt = adapter.forward(
        AuthenticatedInbound.sms(
            sender="+15550001111",
            body="Read this back",
            authentication_receipt="verified-event",
        ),
        render_profile=profile,
        audio_sink=lambda job_id, audio, content_type: routed.append(
            (job_id, audio, content_type)
        ),
    )

    assert _form(http.requests[1]) == {
        "voice_id": ["runtime-voice-id"],
        "style_key": ["future-style"],
        "provider": ["runtime-provider"],
    }
    assert http.requests[1]["url"].endswith("/jobs/job-3/generate")
    assert http.requests[2]["url"].endswith("/jobs/job-3/audio")
    assert all(
        request["headers"]["Authorization"] == "Bearer service-secret"
        for request in http.requests
    )
    assert routed == [("job-3", b"synthetic-audio", "audio/wav")]
    assert receipt.audio_routed is True
    assert "runtime-voice-id" not in repr(profile)
    assert "runtime-voice-id" not in repr(receipt)
    assert "service-secret" not in repr(adapter)


def test_unknown_runtime_provider_is_forwarded_without_catalog_pinning():
    http = FakeHttpTransport(
        [
            _json_response({"message_id": "message-4", "job_id": "job-4"}),
            _json_response({"status": "rendering"}),
        ]
    )
    adapter = VoxTransportAdapter(VoxClient("https://vox.example.test", transport=http))

    receipt = adapter.forward(
        AuthenticatedInbound.email(
            sender="sender@example.test",
            subject="subject",
            body="body",
            authentication_receipt="verified-event",
        ),
        render_profile=VoxRenderProfile(
            voice_id="voice",
            provider="provider-added-after-this-code-shipped",
        ),
    )

    assert receipt.render_status == "rendering"
    assert _form(http.requests[1])["provider"] == [
        "provider-added-after-this-code-shipped"
    ]


def test_render_profile_uses_existing_onepassword_loader(monkeypatch):
    calls = []

    def fake_load_json_secret(**kwargs):
        calls.append(kwargs)
        return {"voice_id": "resolved-voice", "provider": "resolved-provider"}

    monkeypatch.setattr(
        "core.vox_transport.onepassword.load_json_secret", fake_load_json_secret
    )

    profile = load_vox_render_profile()

    assert profile.voice_id == "resolved-voice"
    assert profile.provider == "resolved-provider"
    assert calls == [
        {
            "env_var": "VOX_RENDER_PROFILE_JSON",
            "op_ref_env": "VOX_RENDER_PROFILE_OP_REF",
            "item_env": "OP_VOX_RENDER_PROFILE_ITEM",
            "field_env": "OP_VOX_RENDER_PROFILE_FIELD",
            "vault_env": "OP_VOX_RENDER_PROFILE_VAULT",
        }
    ]


def test_missing_or_malformed_runtime_config_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "core.vox_transport.onepassword.load_json_secret", lambda **_: None
    )
    with pytest.raises(VoxConfigurationError, match="render profile is not configured"):
        load_vox_render_profile()

    monkeypatch.delenv("VOX_BASE_URL", raising=False)
    with pytest.raises(VoxConfigurationError, match="VOX_BASE_URL is required"):
        build_vox_adapter_from_env()

    with pytest.raises(VoxConfigurationError, match="cannot contain credentials"):
        VoxClient("https://user:password@vox.example.test")


def test_http_failure_does_not_echo_message_or_response_body():
    http = FakeHttpTransport(
        [HttpResponse(status=502, body=b"secret upstream detail", headers={})]
    )
    adapter = VoxTransportAdapter(VoxClient("https://vox.example.test", transport=http))

    with pytest.raises(VoxTransportError) as exc_info:
        adapter.forward(
            AuthenticatedInbound.sms(
                sender="private-sender",
                body="private message body",
                authentication_receipt="verified-event",
            )
        )

    detail = str(exc_info.value)
    assert "private-sender" not in detail
    assert "private message body" not in detail
    assert "secret upstream detail" not in detail
