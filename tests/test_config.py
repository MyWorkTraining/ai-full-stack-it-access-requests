"""Settings: defaults, TOML, environment precedence, the credential and the fingerprint."""
from pathlib import Path

import pytest

from access_intake.config import (ConfigError, Settings, api_key, load_schema, load_settings,
                                  settings_fingerprint)
from conftest import ROOT, SCHEMA_PATH

FAKE_KEY = "sk-test-0000-do-not-leak"


def fingerprint(settings, **changes):
    versions = {"prompt_version": "p1", "policy_version": "v1", "schema_sha256": "abc", **changes}
    return settings_fingerprint(settings, **versions)


def test_defaults_use_the_mock_provider():
    settings = load_settings(env={})
    assert settings.provider == "mock"
    assert settings.concurrency == 4
    assert settings.mock_responses == Path("tests/fixtures/mock_responses.json")


def test_precedence_is_toml_then_env_then_overrides(tmp_path):
    toml = tmp_path / "settings.toml"
    toml.write_text('[intake]\nmodel_id = "from-toml"\nconcurrency = 2\ntimeout_s = 30\n')
    env = {"INTAKE_MODEL_ID": "from-env", "INTAKE_TIMEOUT_S": "15"}
    settings = load_settings(toml, env=env, timeout_s=5)
    assert settings.model_id == "from-env"      # env beats TOML
    assert settings.concurrency == 2            # TOML beats default
    assert settings.timeout_s == 5              # keyword override beats env


def test_example_settings_file_loads():
    settings = load_settings(ROOT / "config" / "settings.example.toml", env={})
    assert settings.provider == "mock"


@pytest.mark.parametrize("env", [{"INTAKE_PROVIDER": "openai"}, {"INTAKE_CONCURRENCY": "0"},
                                 {"INTAKE_CONCURRENCY": "four"}, {"INTAKE_TIMEOUT_S": "-1"}])
def test_invalid_settings_are_rejected(env):
    with pytest.raises(ConfigError):
        load_settings(env=env)


def test_unknown_toml_keys_and_missing_files_are_rejected(tmp_path):
    toml = tmp_path / "settings.toml"
    toml.write_text('[intake]\nmodel = "typo"\n')
    with pytest.raises(ConfigError, match="unknown settings"):
        load_settings(toml, env={})
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "absent.toml", env={})


def test_api_key_comes_only_from_the_environment():
    assert api_key({"API_KEY": FAKE_KEY}) == FAKE_KEY
    with pytest.raises(ConfigError, match="API_KEY"):
        api_key({})


def test_credential_never_enters_settings(monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_KEY)
    settings = load_settings()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings.describe())
    assert FAKE_KEY not in fingerprint(settings)


def test_fingerprint_is_stable_and_tracks_what_changes_output():
    base = Settings()
    assert fingerprint(base) == fingerprint(Settings())
    assert fingerprint(Settings(model_id="other")) != fingerprint(base)
    assert fingerprint(Settings(temperature=0.5)) != fingerprint(base)
    assert fingerprint(base, prompt_version="p2") != fingerprint(base)
    assert fingerprint(base, policy_version="v2") != fingerprint(base)
    assert fingerprint(base, schema_sha256="def") != fingerprint(base)


def test_fingerprint_ignores_operational_settings():
    base = Settings()
    assert fingerprint(Settings(timeout_s=5, max_retries=9, concurrency=1, state_dir=Path("x"))) == fingerprint(base)


def test_load_schema_returns_the_file_hash():
    schema, digest = load_schema(SCHEMA_PATH)
    assert schema["additionalProperties"] is False
    assert len(schema["required"]) == 14
    assert len(digest) == 64
