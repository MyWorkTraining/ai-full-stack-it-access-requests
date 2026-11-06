"""Failure handling: provider errors and retries, unusable replies, bad exports, the breaker, credentials."""
import json
import logging

import pytest

from access_intake.llm import MockModelClient, ProviderUnavailable
from access_intake.logging_redact import REDACTED, RedactingFilter, redact
from conftest import ALL_SOURCES, MOCK_RESPONSES, REQUESTS_DIR, read_outputs

FAKE_KEY = "sk-test-0000-do-not-leak-0000"


def test_provider_failure_is_isolated_and_actionable(make_pipeline, tmp_path, sleeps):
    result = make_pipeline(faults="IT-002:unavailable").run(ALL_SOURCES)
    records, queue = read_outputs(tmp_path)
    failed = records["IT-002.json"]
    assert (failed["status"], failed["owner"], failed["next_action"]) == ("error", "IT Service Desk", "retry-extraction")
    assert failed["extraction"] is None
    assert failed["error"]["code"] == "provider-unavailable"
    assert failed["error"]["retry_command"] == "intake retry IT-002.json"
    assert "intake retry IT-002.json" in failed["error"]["message"] and "after 4 attempt(s)" in failed["error"]["message"]
    assert failed["error"]["model_calls"] == 4                  # 1 + max_retries
    assert sleeps == [0.01, 0.02, 0.04]                          # exponential backoff (jitter fixed at 1.0)
    assert failed["last_valid_attempt"] is None
    assert result.states() == {"done": 20, "error": 1}           # the other documents moved on
    entry = next(e for e in queue["entries"] if e["document_id"] == "IT-002.json")
    assert entry["status"] == "error" and entry["error"]["code"] == "provider-unavailable"


def test_timeout_is_retried_then_succeeds(make_pipeline, tmp_path, sleeps):
    result = make_pipeline(faults="IT-001:timeout*1").run([REQUESTS_DIR / "IT-001.json"])
    outcome = result.outcomes["IT-001.json"]
    assert (outcome.state, outcome.model_calls) == ("done", 2)
    assert sleeps == [0.01]
    assert read_outputs(tmp_path)[0]["IT-001.json"]["model_calls"] == 2


def test_rate_limit_honours_retry_after(make_pipeline, sleeps):
    result = make_pipeline(faults="IT-001:rate_limit*2").run([REQUESTS_DIR / "IT-001.json"])
    assert result.outcomes["IT-001.json"].state == "done"
    assert sleeps == [0.0, 0.0]                                  # the provider's Retry-After, not the backoff


def test_backoff_is_capped(make_pipeline, sleeps):
    make_pipeline(faults="IT-001:timeout*5", max_retries=5, backoff_base_s=1, backoff_max_s=3) \
        .run([REQUESTS_DIR / "IT-001.json"])
    assert sleeps == [1, 2, 3, 3, 3]


@pytest.mark.parametrize("fault, code", [("malformed", "malformed-output"), ("schema_violation", "schema-violation"),
                                         ("refusal", "refused"), ("truncated", "truncated"),
                                         ("content_filter", "content-filtered"),
                                         ("ungrounded_quote", "ungrounded-quote"),
                                         ("bad_request", "provider-bad-request")])
def test_unusable_replies_are_errors_without_retry(fault, code, make_pipeline, tmp_path, sleeps):
    result = make_pipeline(faults=f"IT-003:{fault}").run([REQUESTS_DIR / "IT-003.json", REQUESTS_DIR / "IT-001.json"])
    records, _ = read_outputs(tmp_path)
    assert records["IT-003.json"]["error"]["code"] == code
    assert records["IT-003.json"]["status"] == "error" and records["IT-003.json"]["extraction"] is None
    assert result.outcomes["IT-003.json"].model_calls == 1 and sleeps == []
    assert records["IT-001.json"]["status"] == "review"


def test_missing_mock_fixture_is_an_error(make_pipeline, tmp_path):
    fixtures = json.loads(MOCK_RESPONSES.read_text())["documents"]
    del fixtures["IT-003.json"]
    client = MockModelClient(fixtures=fixtures)
    make_pipeline(client=client).run([REQUESTS_DIR / "IT-003.json"])
    assert read_outputs(tmp_path)[0]["IT-003.json"]["error"]["code"] == "mock-fixture-missing"


def test_malformed_export_does_not_stop_the_batch(make_pipeline, tmp_path):
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "IT-900.json").write_bytes(b'{"message": ')
    (batch / "IT-001.json").write_bytes((REQUESTS_DIR / "IT-001.json").read_bytes())
    result = make_pipeline().run([batch])
    records, _ = read_outputs(tmp_path)
    bad = records["IT-900.json"]
    assert bad["error"]["code"] == "malformed-export" and "invalid JSON" in bad["error"]["message"]
    assert bad["source_sha256"] and bad["response_source"] is None and bad["model_calls"] == 0
    assert result.outcomes["IT-001.json"].state == "done"


def test_unexpected_client_exception_is_contained(make_pipeline, tmp_path):
    class Broken(MockModelClient):
        def complete(self, request):
            if request.document_id == "IT-002.json":
                raise RuntimeError("socket closed")
            return super().complete(request)

    result = make_pipeline(client=Broken(MOCK_RESPONSES)).run([REQUESTS_DIR])
    assert read_outputs(tmp_path)[0]["IT-002.json"]["error"]["code"] == "client-error"
    assert result.states() == {"done": 19, "error": 1}


# The circuit breaker and resume after an outage.

def test_outage_halts_the_run_and_resume_finishes_it(make_pipeline, tmp_path):
    outage = make_pipeline(faults="*:unavailable", concurrency=1, max_retries=0, breaker_threshold=3)
    halted = outage.run(ALL_SOURCES)
    assert halted.status == "halted"
    assert halted.states() == {"error": 3, "pending": 18}
    assert f"intake resume {halted.run_id}" in halted.message
    assert outage.store.read_run(halted.run_id)["status"] == "halted"

    healthy = make_pipeline()
    resumed = healthy.resume()
    assert resumed.run_id == halted.run_id
    assert resumed.states() == {"done": 18} and resumed.model_calls == 18
    retried = healthy.retry(errors=True)
    assert retried.states() == {"done": 3}
    records, queue = read_outputs(tmp_path)
    assert queue["total"] == 21 and all(r["status"] != "error" for r in records.values())
    assert healthy.resume().status == "nothing-to-resume"


def test_unconfigured_provider_fails_cleanly_and_halts(make_pipeline, tmp_path, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    result = make_pipeline(provider="provider", concurrency=1).run(ALL_SOURCES)
    assert result.status == "halted" and result.states() == {"error": 5, "pending": 16}
    record = next(iter(read_outputs(tmp_path)[0].values()))
    assert record["error"]["code"] == "provider-not-configured" and "API_KEY" in record["error"]["message"]
    assert record["response_source"] == "unselected/mock-extractor-v1"


# Credentials never reach outputs, state or logs.

def test_api_key_never_appears_in_outputs_state_or_logs(make_pipeline, tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("API_KEY", FAKE_KEY)

    class Echoing(MockModelClient):   # a provider whose error text echoes the credential
        def complete(self, request):
            if request.document_id == "IT-002.json":
                raise ProviderUnavailable(f"upstream 503; request had Authorization: Bearer {FAKE_KEY}")
            return super().complete(request)

    caplog.set_level(logging.INFO, logger="access_intake")
    make_pipeline(client=Echoing(MOCK_RESPONSES), max_retries=0).run(ALL_SOURCES)
    assert FAKE_KEY not in caplog.text and caplog.records
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert FAKE_KEY not in path.read_text(encoding="utf-8", errors="replace"), path
    assert REDACTED in read_outputs(tmp_path)[0]["IT-002.json"]["error"]["message"]


@pytest.mark.parametrize("text", [f"key={FAKE_KEY}", "Authorization: Bearer abcdefghijklmnop", "api_key: 'xyz12345'",
                                  "token=abc123def", "dapi1234567890abcdef", "sk-abcdefghijklmnop"])
def test_redact_removes_credential_shapes(text, monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_KEY)
    cleaned = redact(text)
    assert REDACTED in cleaned or cleaned != text
    assert FAKE_KEY not in cleaned and "abcdefghijklmnop" not in cleaned


def test_redact_leaves_ordinary_text_alone():
    text = "IT-002.json: provider-unavailable after 4 attempt(s); max_output_tokens 2000"
    assert redact(text) == text


def test_redacting_filter_covers_arguments_and_exceptions(monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_KEY)
    record = logging.LogRecord("access_intake", logging.ERROR, __file__, 1, "calling with %s", (FAKE_KEY,), None)
    try:
        raise ValueError(FAKE_KEY)
    except ValueError:
        import sys
        record.exc_info = sys.exc_info()
    RedactingFilter().filter(record)
    assert FAKE_KEY not in record.getMessage() and FAKE_KEY not in (record.exc_text or "")
