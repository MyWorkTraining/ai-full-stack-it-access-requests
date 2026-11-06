"""request-evidence.json: a credential-redacted example model request and the implemented response handling.

`intake evidence [EXPORT] --out deliverables/request-evidence.json` builds the exact ModelRequest the
pipeline sends for one export (default IT-006, which shows instruction-override handling), sends it
to the configured client, validates and routes the reply as `intake run` does, and writes all of it
with every credential redacted. Nothing is written to the review queue.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import policy
from .config import Settings, extraction_fingerprint, load_schema, settings_fingerprint
from .llm import ProviderError, get_client
from .loader import load_document
from .logging_redact import REDACTED, redact
from .prompt import PROMPT_VERSION, build_request
from .validate import ExtractionError, validate_response

RESPONSE_HANDLING = [
    {"stage": "transport", "handling": "ProviderTimeout, ProviderRateLimited (honours Retry-After) and "
     "ProviderUnavailable are retried up to max_retries times with exponential backoff and jitter "
     "(backoff_base_s doubling, capped at backoff_max_s); ProviderBadRequest and ProviderNotConfigured are "
     "not retried. After breaker_threshold consecutive documents cannot reach the service, the run halts "
     "and leaves the rest pending for `intake resume`.",
     "error_codes": ["provider-timeout", "provider-rate-limited", "provider-unavailable", "provider-bad-request",
                     "provider-not-configured", "client-error"]},
    {"stage": "completion status", "handling": "finish_reason refusal, content_filter or length (an interrupted "
     "or truncated reply), or no text, is rejected before any parsing.",
     "error_codes": ["refused", "content-filtered", "truncated", "empty-output"]},
    {"stage": "parse", "handling": "exactly one JSON object (a single ```json fence is accepted); prose or "
     "invalid JSON is rejected.", "error_codes": ["malformed-output"]},
    {"stage": "schema", "handling": "snapshot.schema.json with additionalProperties false and enum values.",
     "error_codes": ["schema-violation"]},
    {"stage": "grounding", "handling": "every supporting quote must be an exact substring of the message; "
     "non-null dates must be real YYYY-MM-DD dates.", "error_codes": ["ungrounded-quote", "invalid-date"]},
    {"stage": "deterministic additions", "handling": "code adds id-mismatch (stated ID vs filename), date-order "
     "(start after end), injection (regex detector, with the matched sentence as a quote), conflict (manager, "
     "environment, permission, start_date or end_date stated with different values and no correction, ignoring "
     "instruction-override sentences and, for permission, change requests: the field is set to null and both "
     "statements quoted) and missing (an unexplained null required fact) issues if the model omitted them; "
     "then a supporting quote contained in another quote is merged into the longer one.", "error_codes": []},
    {"stage": "routing", "handling": "access_intake.policy applies the five office-policy rules in order from "
     "structured values only; nothing approves a request.", "error_codes": []},
    {"stage": "failure record", "handling": "any error gives status error / owner IT Service Desk / next_action "
     "retry-extraction, a null extraction, the error code and message, the exact retry command "
     "(intake retry <document_id>), and a pointer to the last valid attempt, which stays in history.",
     "error_codes": []},
]


def build_evidence(settings: Settings, export_path: str | Path, client=None) -> dict[str, Any]:
    """The evidence object for one export, with credentials redacted."""
    schema, schema_sha256 = load_schema(settings.schema_path)
    document = load_document(export_path)
    request = build_request(document, settings, schema)
    client = client if client is not None else get_client(settings)
    source = getattr(client, "response_source", None) or f"{settings.provider}/{settings.model_id}"
    evidence: dict[str, Any] = {
        "description": "Example model request built by access_intake.prompt.build_request, exactly as `intake run` "
                       "sends it, with the reply and how it was handled. Credentials are redacted.",
        "generated_by": f"intake evidence {Path(export_path).as_posix()}",
        "configuration": {
            "provider": settings.provider, "response_source": source, "model_id": settings.model_id,
            "model_params": settings.model_params(), "max_retries": settings.max_retries,
            "backoff_base_s": settings.backoff_base_s, "backoff_max_s": settings.backoff_max_s,
            "breaker_threshold": settings.breaker_threshold, "concurrency": settings.concurrency,
            "prompt_version": PROMPT_VERSION, "policy_version": policy.POLICY_VERSION,
            "schema_sha256": schema_sha256,
            "settings_fingerprint": settings_fingerprint(settings, prompt_version=PROMPT_VERSION,
                                                         policy_version=policy.POLICY_VERSION,
                                                         schema_sha256=schema_sha256),
            "extraction_fingerprint": extraction_fingerprint(settings, prompt_version=PROMPT_VERSION,
                                                             schema_sha256=schema_sha256)},
        "credential_handling": {
            "source": "environment variable API_KEY, read by the provider client at call time (config.api_key())",
            "stored_in_settings_or_records": False,
            "http_headers_as_sent": {"Authorization": f"Bearer {REDACTED}", "Content-Type": "application/json"},
            "log_redaction": "access_intake.logging_redact removes the API_KEY value and credential shapes from "
                              "logs and stored error text"},
        "request": request.evidence(),
        "response_handling": RESPONSE_HANDLING,
    }
    if hasattr(client, "endpoint_url") and hasattr(client, "body"):   # an HTTP provider: show the wire request
        try:
            evidence["configuration"]["endpoint_url"] = client.endpoint_url()
        except ProviderError as exc:
            evidence["configuration"]["endpoint_url"] = f"not configured: {exc}"
        evidence["credential_handling"]["token_sources"] = ["API_KEY", "DATABRICKS_TOKEN (fallback)"]
        evidence["http_body_as_sent"] = client.body(request)
    try:
        response = client.complete(request)
    except (ProviderError, LookupError) as exc:
        evidence["example_response"] = {"error": f"{type(exc).__name__}: {exc}"}
    else:
        reply: Any = response.text
        try:
            reply = json.loads(response.text) if response.text else None
        except ValueError:
            pass
        evidence["example_response"] = {"response_source": response.response_source,
                                        "finish_reason": response.finish_reason, "refusal": response.refusal,
                                        "body": reply}
        try:
            valid = validate_response(response, document, schema)
        except ExtractionError as exc:
            evidence["handled_outcome"] = {"accepted": False, "error": exc.as_record(),
                                           "route": policy.route_error().as_dict()}
        else:
            decision = policy.route(valid.extraction, valid.flags)
            evidence["handled_outcome"] = {
                "accepted": True, "added_issues": list(valid.added_issues), "added_quotes": list(valid.added_quotes),
                "flags": {"injection": valid.flags.injection, "injection_evidence": list(valid.flags.injection_evidence),
                          "id_mismatch": valid.flags.id_mismatch, "date_order": valid.flags.date_order,
                          "conflicts": list(valid.flags.conflicts)},
                "route": decision.as_dict()}
    return json.loads(redact(json.dumps(evidence, ensure_ascii=False)))


def write_evidence(settings: Settings, export_path: str | Path, out_path: str | Path, client=None) -> dict[str, Any]:
    from .store import write_json
    evidence = build_evidence(settings, export_path, client)
    write_json(Path(out_path), evidence)
    return evidence
