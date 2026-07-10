"""Credential-neutral transport adapter for the VOX ingest seam.

The account-owning transport authenticates the incoming Gmail or Twilio event
before constructing :class:`AuthenticatedInbound`. This module only forwards
that normalized event to VOX, optionally asks VOX to render the pending job,
and hands the resulting audio to a caller-supplied sink.

No provider account creation, voice samples, or provider API keys belong here.
Runtime selectors and an optional VOX service token are resolved through UMA's
existing 1Password-backed loader.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from auth import onepassword


class VoxTransportError(RuntimeError):
    """Raised when a VOX transport request cannot be completed safely."""


class VoxConfigurationError(VoxTransportError):
    """Raised when required runtime configuration is absent or malformed."""


@dataclass(frozen=True)
class HttpResponse:
    """Small response value used by the injectable HTTP boundary."""

    status: int
    body: bytes
    headers: Mapping[str, str]


class HttpTransport(Protocol):
    """HTTP boundary; tests and deployments may supply their own transport."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HttpResponse: ...


class UrllibHttpTransport:
    """Standard-library HTTP implementation with secret-safe failures."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> HttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator-owned URL
                return HttpResponse(
                    status=int(response.status),
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            raise VoxTransportError(f"VOX returned HTTP {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise VoxTransportError("VOX is unreachable") from None


@dataclass(frozen=True)
class AuthenticatedInbound:
    """A normalized event whose owning transport has already authenticated it.

    ``authentication_receipt`` is an opaque local receipt reference. It is
    required as a fail-closed boundary marker, never sent to VOX, and excluded
    from representations to avoid accidental logging.
    """

    source: str
    sender: str = field(repr=False)
    body: str = field(repr=False)
    subject: Optional[str] = field(default=None, repr=False)
    authentication_receipt: str = field(default="", repr=False)

    @classmethod
    def email(
        cls,
        *,
        sender: str,
        subject: str,
        body: str,
        authentication_receipt: str,
    ) -> "AuthenticatedInbound":
        return cls(
            source="email",
            sender=sender,
            subject=subject,
            body=body,
            authentication_receipt=authentication_receipt,
        )

    @classmethod
    def sms(
        cls,
        *,
        sender: str,
        body: str,
        authentication_receipt: str,
    ) -> "AuthenticatedInbound":
        return cls(
            source="sms",
            sender=sender,
            body=body,
            authentication_receipt=authentication_receipt,
        )

    def validate(self) -> None:
        if self.source not in {"email", "sms"}:
            raise VoxTransportError("unsupported VOX ingest source")
        if not self.authentication_receipt.strip():
            raise VoxTransportError("authenticated transport receipt is required")
        if not self.sender.strip():
            raise VoxTransportError("sender is required")
        if not self.body.strip():
            raise VoxTransportError("message body is required")


@dataclass(frozen=True)
class VoxRenderProfile:
    """Runtime render selectors resolved from the credential layer."""

    voice_id: str = field(repr=False)
    style_key: Optional[str] = None
    provider: Optional[str] = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "VoxRenderProfile":
        voice_id = value.get("voice_id")
        if not isinstance(voice_id, str) or not voice_id.strip():
            raise VoxConfigurationError("VOX render profile is missing voice_id")
        return cls(
            voice_id=voice_id.strip(),
            style_key=_optional_text(value.get("style_key"), "style_key"),
            provider=_optional_text(value.get("provider"), "provider"),
        )


@dataclass(frozen=True)
class VoxForwardReceipt:
    """Secret- and message-free receipt for one forwarding attempt."""

    source: str
    message_id: str
    job_id: str
    render_status: str
    audio_routed: bool


AudioSink = Callable[[str, bytes, Optional[str]], None]


class VoxClient:
    """Thin client for only the VOX Phase 3a receiver/job contract."""

    def __init__(
        self,
        base_url: str,
        *,
        service_token: Optional[str] = None,
        timeout: float = 30.0,
        transport: Optional[HttpTransport] = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise VoxConfigurationError("VOX_BASE_URL must be an http(s) URL")
        if parsed.username or parsed.password:
            raise VoxConfigurationError("VOX_BASE_URL cannot contain credentials")
        if parsed.query or parsed.fragment:
            raise VoxConfigurationError(
                "VOX_BASE_URL cannot contain a query or fragment"
            )
        if timeout <= 0:
            raise VoxConfigurationError("VOX HTTP timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout
        self._transport = transport or UrllibHttpTransport()

    def ingest(self, message: AuthenticatedInbound) -> tuple[str, str]:
        message.validate()
        if message.source == "email":
            payload = {
                "sender": message.sender,
                "subject": message.subject or "",
                "body": message.body,
            }
        else:
            payload = {"From": message.sender, "Body": message.body}
        response = self._json_request("POST", f"/ingest/{message.source}", payload)
        return (
            _required_response_text(response, "message_id"),
            _required_response_text(response, "job_id"),
        )

    def generate(self, job_id: str, profile: VoxRenderProfile) -> str:
        payload = {"voice_id": profile.voice_id}
        if profile.style_key:
            payload["style_key"] = profile.style_key
        if profile.provider:
            payload["provider"] = profile.provider
        response = self._json_request(
            "POST",
            f"/jobs/{quote(_required_identifier(job_id), safe='')}/generate",
            payload,
        )
        return _required_response_text(response, "status")

    def audio(self, job_id: str) -> tuple[bytes, Optional[str]]:
        response = self._request(
            "GET",
            f"/jobs/{quote(_required_identifier(job_id), safe='')}/audio",
            None,
        )
        content_type = next(
            (
                value
                for key, value in response.headers.items()
                if key.lower() == "content-type"
            ),
            None,
        )
        return response.body, content_type

    def _json_request(
        self,
        method: str,
        path: str,
        form: Mapping[str, str],
    ) -> Mapping[str, object]:
        response = self._request(method, path, form)
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise VoxTransportError("VOX returned an invalid JSON response") from None
        if not isinstance(value, dict):
            raise VoxTransportError("VOX returned an invalid JSON response")
        return value

    def _request(
        self,
        method: str,
        path: str,
        form: Optional[Mapping[str, str]],
    ) -> HttpResponse:
        headers = {"Accept": "application/json"}
        body = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urlencode(form).encode("utf-8")
        if self._service_token:
            headers["Authorization"] = f"Bearer {self._service_token}"
        response = self._transport.request(
            method,
            f"{self._base_url}{path}",
            headers=headers,
            body=body,
            timeout=self._timeout,
        )
        if not 200 <= response.status < 300:
            raise VoxTransportError(f"VOX returned HTTP {response.status}")
        return response


class VoxTransportAdapter:
    """Forward authenticated inbound messages and optionally route rendered audio."""

    def __init__(self, client: VoxClient) -> None:
        self._client = client

    def forward(
        self,
        message: AuthenticatedInbound,
        *,
        render_profile: Optional[VoxRenderProfile] = None,
        audio_sink: Optional[AudioSink] = None,
    ) -> VoxForwardReceipt:
        if audio_sink is not None and render_profile is None:
            raise VoxConfigurationError("audio routing requires a VOX render profile")
        message_id, job_id = self._client.ingest(message)
        status = "pending"
        routed = False
        if render_profile is not None:
            status = self._client.generate(job_id, render_profile)
            if status == "done" and audio_sink is not None:
                audio, content_type = self._client.audio(job_id)
                audio_sink(job_id, audio, content_type)
                routed = True
        return VoxForwardReceipt(
            source=message.source,
            message_id=message_id,
            job_id=job_id,
            render_status=status,
            audio_routed=routed,
        )


def load_vox_render_profile() -> VoxRenderProfile:
    """Resolve render selectors through UMA's existing credential loader."""

    value = onepassword.load_json_secret(
        env_var="VOX_RENDER_PROFILE_JSON",
        op_ref_env="VOX_RENDER_PROFILE_OP_REF",
        item_env="OP_VOX_RENDER_PROFILE_ITEM",
        field_env="OP_VOX_RENDER_PROFILE_FIELD",
        vault_env="OP_VOX_RENDER_PROFILE_VAULT",
    )
    if value is None:
        raise VoxConfigurationError("VOX render profile is not configured")
    return VoxRenderProfile.from_mapping(value)


def build_vox_adapter_from_env(
    *,
    transport: Optional[HttpTransport] = None,
) -> VoxTransportAdapter:
    """Build an adapter from non-secret env config plus a 1Password-backed token."""

    base_url = os.getenv("VOX_BASE_URL", "").strip()
    if not base_url:
        raise VoxConfigurationError("VOX_BASE_URL is required")
    service_token = onepassword.load_secret(
        env_var="VOX_ACCESS_TOKEN",
        op_ref_env="VOX_ACCESS_TOKEN_OP_REF",
        item_env="OP_VOX_ACCESS_TOKEN_ITEM",
        field_env="OP_VOX_ACCESS_TOKEN_FIELD",
        vault_env="OP_VOX_ACCESS_TOKEN_VAULT",
    )
    timeout_raw = os.getenv("VOX_HTTP_TIMEOUT_SECONDS", "30")
    try:
        timeout = float(timeout_raw)
    except ValueError:
        raise VoxConfigurationError(
            "VOX_HTTP_TIMEOUT_SECONDS must be numeric"
        ) from None
    return VoxTransportAdapter(
        VoxClient(
            base_url,
            service_token=service_token,
            timeout=timeout,
            transport=transport,
        )
    )


def _optional_text(value: object, field_name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VoxConfigurationError(f"VOX render profile {field_name} must be text")
    normalized = value.strip()
    return normalized or None


def _required_response_text(value: Mapping[str, object], field_name: str) -> str:
    raw = value.get(field_name)
    if not isinstance(raw, str) or not raw.strip():
        raise VoxTransportError(f"VOX response is missing {field_name}")
    return raw.strip()


def _required_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise VoxTransportError("VOX job id is required")
    return normalized
