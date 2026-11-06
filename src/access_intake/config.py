"""Settings for intake runs: defaults, an optional TOML file, then environment overrides.

Precedence (lowest to highest): built-in defaults < TOML file ([intake] table) < INTAKE_* environment
variables < explicit keyword overrides. The provider credential is never part of Settings: it is read
from the API_KEY environment variable at call time (api_key()), so it cannot leak into the settings
fingerprint, a repr, a log line or an output record.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

API_KEY_ENV = "API_KEY"
# mock: canned fixtures; databricks: a Databricks Model Serving / Foundation Model API endpoint;
# provider: the unselected-provider placeholder (fails cleanly).
PROVIDERS = ("mock", "databricks", "provider")
STRUCTURED_OUTPUTS = ("json_schema", "json_object", "none")
MOCK_MODEL_ID = "mock-extractor-v1"


def optional_float(value: Any) -> float | None:
    """A float, or None for "none"/"default"/"" (send no value and let the endpoint use its default)."""
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "none", "default", "null")):
        return None
    return float(value)


# INTAKE_* variable -> (Settings field, converter)
ENV_OVERRIDES: dict[str, tuple[str, Any]] = {
    "INTAKE_PROVIDER": ("provider", str),
    "INTAKE_MODEL_ID": ("model_id", str),
    "INTAKE_TEMPERATURE": ("temperature", optional_float),
    "INTAKE_STRUCTURED_OUTPUT": ("structured_output", str),
    "INTAKE_DATABRICKS_HOST": ("databricks_host", str),
    "INTAKE_MAX_OUTPUT_TOKENS": ("max_output_tokens", int),
    "INTAKE_TIMEOUT_S": ("timeout_s", float),
    "INTAKE_MAX_RETRIES": ("max_retries", int),
    "INTAKE_CONCURRENCY": ("concurrency", int),
    "INTAKE_BACKOFF_BASE_S": ("backoff_base_s", float),
    "INTAKE_BACKOFF_MAX_S": ("backoff_max_s", float),
    "INTAKE_BREAKER_THRESHOLD": ("breaker_threshold", int),
    "INTAKE_CLAIM_TTL_S": ("claim_ttl_s", float),
    "INTAKE_STATE_DIR": ("state_dir", Path),
    "INTAKE_OUTPUT_DIR": ("output_dir", Path),
    "INTAKE_SCHEMA_PATH": ("schema_path", Path),
    "INTAKE_MOCK_RESPONSES": ("mock_responses", Path),
    "MOCK_FAULTS": ("mock_faults", str),
}


class ConfigError(ValueError):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Processing settings. Fields marked fingerprint=True change how records are produced."""

    provider: str = field(default="mock", metadata={"fingerprint": True})
    model_id: str = field(default=MOCK_MODEL_ID, metadata={"fingerprint": True})
    temperature: float | None = field(default=0.0, metadata={"fingerprint": True})   # None: endpoint default
    max_output_tokens: int = field(default=2000, metadata={"fingerprint": True})
    # Fingerprinted only when changed from the default, so adding the option did not invalidate records.
    structured_output: str = field(default="json_schema", metadata={"fingerprint": "if-set"})
    databricks_host: str = ""         # workspace URL; empty: the DATABRICKS_HOST environment variable
    timeout_s: float = 60.0
    max_retries: int = 3
    concurrency: int = 4
    backoff_base_s: float = 1.0       # first retry wait; doubles per retry, with jitter
    backoff_max_s: float = 30.0       # cap on one wait
    breaker_threshold: int = 5        # consecutive unavailable documents before a run halts
    claim_ttl_s: float = 900.0        # a claim older than this is treated as abandoned
    state_dir: Path = Path("state")
    output_dir: Path = Path("deliverables")
    schema_path: Path = Path("snapshot.schema.json")
    mock_responses: Path = Path("tests/fixtures/mock_responses.json")
    mock_faults: str = ""

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ConfigError(f"provider must be one of {PROVIDERS}, got {self.provider!r}")
        if not self.model_id:
            raise ConfigError("model_id must not be empty")
        if self.provider == "databricks" and self.model_id == MOCK_MODEL_ID:
            raise ConfigError("provider databricks needs model_id set to a serving endpoint name, e.g. "
                              "INTAKE_MODEL_ID=databricks-claude-sonnet-4-5")
        if self.structured_output not in STRUCTURED_OUTPUTS:
            raise ConfigError(f"structured_output must be one of {STRUCTURED_OUTPUTS}, got {self.structured_output!r}")
        if self.concurrency < 1:
            raise ConfigError("concurrency must be at least 1")
        if self.max_retries < 0:
            raise ConfigError("max_retries must be 0 or more")
        if self.timeout_s <= 0:
            raise ConfigError("timeout_s must be positive")
        if self.backoff_base_s < 0 or self.backoff_max_s < 0:
            raise ConfigError("backoff times must be 0 or more")
        if self.breaker_threshold < 1:
            raise ConfigError("breaker_threshold must be at least 1")
        if self.claim_ttl_s <= 0:
            raise ConfigError("claim_ttl_s must be positive")

    def model_params(self) -> dict[str, Any]:
        """The request parameters sent with each model call."""
        return {"temperature": self.temperature, "max_output_tokens": self.max_output_tokens,
                "timeout_s": self.timeout_s}

    def fingerprint_inputs(self) -> dict[str, Any]:
        """The settings that affect extraction output (timeouts, retries and paths do not)."""
        inputs = {}
        for f in dataclasses.fields(self):
            mode = f.metadata.get("fingerprint")
            if mode is True or (mode == "if-set" and getattr(self, f.name) != f.default):
                inputs[f.name] = getattr(self, f.name)
        return inputs

    def describe(self) -> dict[str, Any]:
        """All settings as JSON-safe values, for run manifests and evidence (no credential)."""
        return {k: str(v) if isinstance(v, Path) else v for k, v in dataclasses.asdict(self).items()}


def load_settings(path: str | Path | None = None, env: Mapping[str, str] | None = None,
                  **overrides: Any) -> Settings:
    """Build Settings from defaults, an optional TOML file, the environment and keyword overrides.

    Args:
        path: a TOML file with an [intake] table. None: no file. A missing file is an error.
        env: the environment to read INTAKE_* overrides from. None: os.environ.
        **overrides: final values for Settings fields (None values are ignored).
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    names = {f.name: f for f in dataclasses.fields(Settings)}
    if path is not None:
        path = Path(path)
        if not path.is_file():
            raise ConfigError(f"settings file not found: {path}")
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("intake", {})
        unknown = set(table) - set(names)
        if unknown:
            raise ConfigError(f"unknown settings in {path}: {sorted(unknown)}")
        values.update(table)
    for var, (name, convert) in ENV_OVERRIDES.items():
        if env.get(var) not in (None, ""):
            try:
                values[name] = convert(env[var])
            except ValueError as exc:
                raise ConfigError(f"{var}={env[var]!r} is not a valid {convert.__name__}") from exc
    values.update({k: v for k, v in overrides.items() if v is not None})
    if "temperature" in values:
        try:
            values["temperature"] = optional_float(values["temperature"])
        except ValueError as exc:
            raise ConfigError(f"temperature {values['temperature']!r} is not a number or 'none'") from exc
    for name in ("state_dir", "output_dir", "schema_path", "mock_responses"):
        if name in values:
            values[name] = Path(values[name])
    return Settings(**values)


def api_key(env: Mapping[str, str] | None = None) -> str:
    """The provider credential from API_KEY. Raises ConfigError when it is not set."""
    env = os.environ if env is None else env
    key = env.get(API_KEY_ENV, "")
    if not key:
        raise ConfigError(f"environment variable {API_KEY_ENV} is not set")
    return key


def load_schema(path: str | Path) -> tuple[dict[str, Any], str]:
    """The record schema and the SHA-256 of its file bytes."""
    raw = Path(path).read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def extraction_fingerprint(settings: Settings, *, prompt_version: str, schema_sha256: str) -> str:
    """A SHA-256 over what changes the extraction itself (not routing): a stored extraction with the
    same source hash and extraction fingerprint can be reused, and only re-routed, without a model call."""
    payload = {"settings": settings.fingerprint_inputs(), "prompt_version": prompt_version,
               "schema_sha256": schema_sha256}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def settings_fingerprint(settings: Settings, *, prompt_version: str, policy_version: str,
                         schema_sha256: str) -> str:
    """A stable SHA-256 over everything that changes how a record is produced or routed.

    Changing the provider, model, model parameters, prompt, schema or policy changes the
    fingerprint, so the next run refreshes the affected documents.
    """
    payload = {"settings": settings.fingerprint_inputs(), "prompt_version": prompt_version,
               "policy_version": policy_version, "schema_sha256": schema_sha256}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
