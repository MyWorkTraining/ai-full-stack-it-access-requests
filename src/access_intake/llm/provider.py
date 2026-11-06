"""The real model provider: a placeholder until one is selected.

To wire in a provider (OpenAI, Anthropic, Databricks Foundation Model APIs, ...), implement
ProviderModelClient.complete() and change nothing else:

1. Read the credential with access_intake.config.api_key() (environment variable API_KEY) on each
   call; never store it on the instance, log it or put it in ModelResponse.raw.
2. Send request.system_prompt and request.user_content as two messages, request.schema as the
   structured-output / JSON-schema response format, request.model_id and request.params
   (temperature, max_output_tokens; timeout_s as the HTTP timeout).
3. Map the provider's outcome:
     reply received     -> ModelResponse(text, finish_reason, f"{PROVIDER_NAME}/{model_id}", refusal, raw)
                           with finish_reason "stop", "length" (cut off), "refusal" or "content_filter"
     HTTP timeout       -> raise ProviderTimeout
     HTTP 429           -> raise ProviderRateLimited(retry_after=<Retry-After header, seconds>)
     HTTP 5xx / network -> raise ProviderUnavailable
     other HTTP 4xx     -> raise ProviderBadRequest
4. Put a credential-redacted copy of the provider payload in raw (request-evidence.json uses it).
"""
from __future__ import annotations

from ..config import ConfigError, Settings, api_key
from .base import ModelRequest, ModelResponse, ProviderNotConfigured

PROVIDER_NAME = "unselected"


class ProviderModelClient:
    """The ModelClient for the selected provider. Not implemented yet: every call fails cleanly."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def response_source(self) -> str:
        return f"{PROVIDER_NAME}/{self.settings.model_id}"

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            api_key()   # fail with a clear message before any network work when the key is missing
        except ConfigError as exc:
            raise ProviderNotConfigured(str(exc)) from None
        raise ProviderNotConfigured(
            "no model provider is implemented yet; use provider 'mock' (INTAKE_PROVIDER=mock) or implement "
            "access_intake/llm/provider.py")
