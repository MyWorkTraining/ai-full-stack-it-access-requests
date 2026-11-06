"""A ModelClient for Databricks-hosted models (Model Serving / Foundation Model APIs).

It calls the chat-completions interface of a serving endpoint:

    POST {host}/serving-endpoints/{model_id}/invocations
    Authorization: Bearer <API_KEY>
    {"messages": [system, user], "max_tokens": ..., "temperature": ..., "response_format": {...}}

Configuration (see access_intake.config):
  provider = "databricks"       select this client (INTAKE_PROVIDER=databricks); "mock" selects the mock
  model_id                      the serving endpoint name, e.g. databricks-claude-sonnet-4-5
  databricks_host               the workspace URL (INTAKE_DATABRICKS_HOST), else DATABRICKS_HOST
  structured_output             json_schema (default: strict snapshot.schema.json), json_object, or none
  temperature                   a number, or none to send no temperature (some models accept only the default)
  max_output_tokens, timeout_s  sent as max_tokens and used as the HTTP timeout
Credential: the API_KEY environment variable (a Databricks personal access or service-principal token),
else DATABRICKS_TOKEN. It is read on every call, sent only in the Authorization header, and never stored,
logged or copied into ModelResponse.raw.

Outcome mapping: HTTP 200 -> ModelResponse with finish_reason stop / length / refusal / content_filter;
timeout -> ProviderTimeout; 429 -> ProviderRateLimited (Retry-After honoured); 5xx and network
errors -> ProviderUnavailable; 401/403/404 -> ProviderNotConfigured (credential, permission or endpoint
name; every document would fail, so it counts towards the breaker); other 4xx -> ProviderBadRequest.
Only the standard library is used (urllib), so no extra dependency is needed.
"""
from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from ..config import API_KEY_ENV, Settings
from ..logging_redact import redact
from .base import (ModelRequest, ModelResponse, ProviderBadRequest, ProviderNotConfigured, ProviderRateLimited,
                   ProviderTimeout, ProviderUnavailable)

PROVIDER_NAME = "databricks"
FALLBACK_TOKEN_ENV = "DATABRICKS_TOKEN"
HOST_ENV = "DATABRICKS_HOST"
SCHEMA_NAME = "access_request"

# A transport sends one POST and returns (status, headers, body). It raises only for network failures.
Transport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, Mapping[str, str], bytes]]


def urllib_transport(url: str, headers: Mapping[str, str], body: bytes, timeout: float) \
        -> tuple[int, Mapping[str, str], bytes]:
    """POST with the standard library; HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read() or b""


def _finish_reason(raw: Any, message: Mapping[str, Any]) -> str:
    if message.get("refusal"):
        return "refusal"
    reason = str(raw or "stop").lower()
    if reason in ("length", "max_tokens", "max_output_tokens"):
        return "length"
    if reason in ("content_filter", "safety", "blocked"):
        return "content_filter"
    if reason == "refusal":
        return "refusal"
    return "stop"     # stop, end_turn, eos, stop_sequence


def _text(content: Any) -> str | None:
    """Message content as text: a string, or the text parts of a list of content blocks."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text", "") for block in content
                 if isinstance(block, dict) and block.get("type") in (None, "text", "output_text")]
        return "".join(parts) if parts else None
    return None


def _error_detail(body: bytes) -> str:
    try:
        data = json.loads(body)
        detail = data.get("message") or data.get("error", {}).get("message") or data.get("error_code") or body
    except (ValueError, AttributeError):
        detail = body
    text = detail.decode("utf-8", "replace") if isinstance(detail, bytes) else str(detail)
    return redact(" ".join(text.split()))[:300]


class DatabricksModelClient:
    """Calls a Databricks serving endpoint for each ModelRequest."""

    def __init__(self, settings: Settings, *, transport: Transport | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.transport = transport or urllib_transport
        self._env = env

    @property
    def response_source(self) -> str:
        return f"{PROVIDER_NAME}/{self.settings.model_id}"

    # Configuration ---------------------------------------------------------------------------

    def _environ(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def host(self) -> str:
        host = (self.settings.databricks_host or self._environ().get(HOST_ENV, "")).strip().rstrip("/")
        if not host:
            raise ProviderNotConfigured(f"no Databricks workspace URL: set INTAKE_DATABRICKS_HOST or {HOST_ENV}")
        return host if host.startswith(("https://", "http://")) else "https://" + host

    def _token(self) -> str:
        env = self._environ()
        token = env.get(API_KEY_ENV) or env.get(FALLBACK_TOKEN_ENV)
        if not token:
            raise ProviderNotConfigured(f"environment variable {API_KEY_ENV} is not set (a Databricks token)")
        return token

    def endpoint_url(self) -> str:
        return f"{self.host()}/serving-endpoints/{self.settings.model_id}/invocations"

    def body(self, request: ModelRequest) -> dict[str, Any]:
        """The JSON body sent for a request (no credential)."""
        body: dict[str, Any] = {
            "messages": [{"role": "system", "content": request.system_prompt},
                         {"role": "user", "content": request.user_content}],
            "max_tokens": int(request.params.get("max_output_tokens", self.settings.max_output_tokens)),
        }
        temperature = request.params.get("temperature", self.settings.temperature)
        if temperature is not None:
            body["temperature"] = temperature
        mode = self.settings.structured_output
        if mode == "json_schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": SCHEMA_NAME, "schema": request.schema, "strict": True}}
        elif mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        return body

    # The call --------------------------------------------------------------------------------

    def complete(self, request: ModelRequest) -> ModelResponse:
        url = self.endpoint_url()
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        payload = json.dumps(self.body(request)).encode("utf-8")
        timeout = float(request.params.get("timeout_s", self.settings.timeout_s))
        name = f"endpoint {self.settings.model_id}"
        try:
            status, response_headers, raw = self.transport(url, headers, payload, timeout)
        except (TimeoutError, socket.timeout) as exc:
            raise ProviderTimeout(f"{name}: no reply within {timeout:g}s") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ProviderTimeout(f"{name}: no reply within {timeout:g}s") from None
            raise ProviderUnavailable(f"{name}: network error ({redact(str(exc.reason))})") from None
        except OSError as exc:
            raise ProviderUnavailable(f"{name}: network error ({redact(str(exc))})") from None

        if status == 429:
            retry_after = None
            value = {k.lower(): v for k, v in response_headers.items()}.get("retry-after")
            try:
                retry_after = float(value) if value is not None else None
            except ValueError:
                retry_after = None
            raise ProviderRateLimited(f"{name}: rate limited (HTTP 429): {_error_detail(raw)}", retry_after=retry_after)
        if status >= 500:
            raise ProviderUnavailable(f"{name}: HTTP {status}: {_error_detail(raw)}")
        if status in (401, 403, 404):
            raise ProviderNotConfigured(f"{name}: HTTP {status} (check the token, its permission on the endpoint, "
                                        f"and the endpoint name): {_error_detail(raw)}")
        if status >= 400:
            raise ProviderBadRequest(f"{name}: HTTP {status}: {_error_detail(raw)}")

        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderUnavailable(f"{name}: HTTP {status} with an unreadable body") from None
        finish = _finish_reason(choice.get("finish_reason"), message)
        refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else None
        raw_summary = {"endpoint": self.settings.model_id, "id": data.get("id"), "model": data.get("model"),
                       "finish_reason": choice.get("finish_reason"), "usage": data.get("usage")}
        return ModelResponse(text=_text(message.get("content")), finish_reason=finish,
                             response_source=self.response_source, refusal=refusal, raw=raw_summary)
