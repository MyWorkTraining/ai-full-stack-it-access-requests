"""The one interface every model call goes through, and the errors a client may raise.

A client turns a ModelRequest into a ModelResponse. It reports transport-level failures by raising
one of the ProviderError subclasses; a reply that arrived but is unusable (refusal, truncation,
content filter) is returned as a ModelResponse with the matching finish_reason, and the validator
decides what to do with it. Clients never parse or route the extraction themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

FINISH_REASONS = ("stop", "length", "refusal", "content_filter")


@dataclass(frozen=True)
class ModelRequest:
    """One extraction request for one document."""

    document_id: str
    source_sha256: str
    system_prompt: str
    user_content: str          # the message, wrapped in <message>...</message>
    schema: dict[str, Any]     # snapshot.schema.json, sent as the structured-output schema
    model_id: str
    params: dict[str, Any] = field(default_factory=dict)   # temperature, max_output_tokens, timeout_s

    def evidence(self) -> dict[str, Any]:
        """A JSON-safe description of the request for request-evidence.json. Holds no credential."""
        return {"document_id": self.document_id, "source_sha256": self.source_sha256,
                "model_id": self.model_id, "params": dict(self.params),
                "messages": [{"role": "system", "content": self.system_prompt},
                             {"role": "user", "content": self.user_content}],
                "response_format": {"type": "json_schema", "schema": self.schema}}


@dataclass(frozen=True)
class ModelResponse:
    """What the model returned for one request."""

    text: str | None           # the raw reply text (expected: one JSON object)
    finish_reason: str         # one of FINISH_REASONS
    response_source: str       # "mock", or "<provider>/<model_id>"
    refusal: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)   # credential-redacted provider payload

    def __post_init__(self) -> None:
        if self.finish_reason not in FINISH_REASONS:
            raise ValueError(f"unknown finish_reason {self.finish_reason!r}")


@runtime_checkable
class ModelClient(Protocol):
    """Anything that can answer a ModelRequest."""

    def complete(self, request: ModelRequest) -> ModelResponse:
        ...


class ProviderError(Exception):
    """A model call failed before a usable reply arrived.

    Attributes:
        code: a stable error code written into the record's error.
        retryable: whether the pipeline should retry with backoff.
    """

    code = "provider-error"
    retryable = False


class ProviderTimeout(ProviderError):
    code = "provider-timeout"
    retryable = True


class ProviderRateLimited(ProviderError):
    code = "provider-rate-limited"
    retryable = True

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    code = "provider-unavailable"
    retryable = True


class ProviderBadRequest(ProviderError):
    code = "provider-bad-request"
    retryable = False


class ProviderNotConfigured(ProviderError):
    """No real provider is wired in yet, or its credential is missing."""

    code = "provider-not-configured"
    retryable = False
