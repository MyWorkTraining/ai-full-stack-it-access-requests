"""The Databricks serving-endpoint client, against a fake HTTP transport (no network), and the switch
between it and the mock through configuration."""
import json
import re
import socket
import urllib.error

import pytest

from access_intake.config import ConfigError, Settings, load_settings
from access_intake.llm import (DatabricksModelClient, MockModelClient, ProviderBadRequest, ProviderModelClient,
                               ProviderNotConfigured, ProviderRateLimited, ProviderTimeout, ProviderUnavailable,
                               get_client)
from access_intake.pipeline import Pipeline
from access_intake.prompt import build_request
from conftest import ALL_SOURCES, MOCK_RESPONSES, Sleeps, make_request, make_settings, read_outputs

TOKEN = "dapi-test-0000-do-not-leak-0000"
ENV = {"API_KEY": TOKEN, "DATABRICKS_HOST": "adb-123.4.azuredatabricks.net"}
ENDPOINT = "databricks-claude-sonnet-4-5"
FIXTURES = json.loads(MOCK_RESPONSES.read_text(encoding="utf-8"))["documents"]


def completion(content, finish_reason="stop", **message):
    return {"id": "chatcmpl-1", "model": ENDPOINT, "usage": {"prompt_tokens": 900, "completion_tokens": 200},
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant", "content": content, **message}}]}


class FakeTransport:
    """Records each POST; replies with a status/body per call, or the mock fixture for the document."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body), "timeout": timeout})
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            status, payload, *rest = reply
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            return status, (rest[0] if rest else {}), raw
        document_id = re.search(r"Filename: (\S+)", json.loads(body)["messages"][1]["content"]).group(1)
        return 200, {}, json.dumps(completion(json.dumps(FIXTURES[document_id]["extraction"]))).encode()


def client(transport, env=ENV, **changes):
    settings = Settings(provider="databricks", model_id=ENDPOINT, **changes)
    return DatabricksModelClient(settings, transport=transport, env=env)


@pytest.fixture
def request_(load):
    return make_request(load("IT-001.json"), model_id=ENDPOINT)


# Switching between clients through configuration.

def test_configuration_selects_the_client():
    assert isinstance(get_client(Settings(mock_responses=MOCK_RESPONSES)), MockModelClient)
    assert isinstance(get_client(Settings(provider="databricks", model_id=ENDPOINT)), DatabricksModelClient)
    assert isinstance(get_client(Settings(provider="provider")), ProviderModelClient)
    settings = load_settings(env={"INTAKE_PROVIDER": "databricks", "INTAKE_MODEL_ID": ENDPOINT,
                                  "INTAKE_TEMPERATURE": "none", "INTAKE_STRUCTURED_OUTPUT": "json_object",
                                  "INTAKE_DATABRICKS_HOST": "https://example.cloud.databricks.com"})
    assert (settings.provider, settings.model_id, settings.temperature, settings.structured_output,
            settings.databricks_host) == ("databricks", ENDPOINT, None, "json_object",
                                          "https://example.cloud.databricks.com")


def test_databricks_needs_a_real_endpoint_name():
    with pytest.raises(ConfigError, match="serving endpoint name"):
        Settings(provider="databricks")
    with pytest.raises(ConfigError, match="structured_output"):
        Settings(structured_output="xml")


def test_new_options_at_their_defaults_do_not_change_the_fingerprint():
    assert "structured_output" not in Settings().fingerprint_inputs()
    assert Settings(structured_output="json_object").fingerprint_inputs()["structured_output"] == "json_object"
    assert "databricks_host" not in Settings(databricks_host="x").fingerprint_inputs()


# The request on the wire.

def test_request_url_headers_and_body(request_, schema):
    transport = FakeTransport()
    client(transport).complete(request_)
    (call,) = transport.calls
    assert call["url"] == f"https://adb-123.4.azuredatabricks.net/serving-endpoints/{ENDPOINT}/invocations"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = call["body"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == request_.system_prompt
    assert body["messages"][1]["content"] == request_.user_content
    assert body["max_tokens"] == 2000 and body["temperature"] == 0.0
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "access_request",
                                                                          "schema": schema, "strict": True}}
    assert call["timeout"] == 60.0


def test_temperature_none_and_other_structured_output_modes(load):
    transport = FakeTransport()
    for mode, expected in (("json_object", {"type": "json_object"}), ("none", None)):
        settings = Settings(provider="databricks", model_id=ENDPOINT, temperature=None, structured_output=mode)
        DatabricksModelClient(settings, transport=transport, env=ENV).complete(
            build_request(load("IT-001.json"), settings, {}))
        body = transport.calls[-1]["body"]
        assert "temperature" not in body
        assert body.get("response_format") == expected


def test_host_from_settings_wins_and_scheme_is_added(request_):
    transport = FakeTransport()
    client(transport, databricks_host="myws.cloud.databricks.com/").complete(request_)
    assert transport.calls[0]["url"].startswith("https://myws.cloud.databricks.com/serving-endpoints/")


def test_databricks_token_is_the_fallback_credential(request_):
    transport = FakeTransport()
    client(transport, env={"DATABRICKS_TOKEN": "dapi-fallback-0000", "DATABRICKS_HOST": "h"}).complete(request_)
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer dapi-fallback-0000"


@pytest.mark.parametrize("env, message", [({"DATABRICKS_HOST": "h"}, "API_KEY"), ({"API_KEY": TOKEN}, "workspace URL")])
def test_missing_configuration_fails_before_any_call(env, message, request_):
    transport = FakeTransport()
    with pytest.raises(ProviderNotConfigured, match=message):
        client(transport, env=env).complete(request_)
    assert transport.calls == []


# The reply.

def test_successful_reply(request_):
    extraction = FIXTURES["IT-001.json"]["extraction"]
    response = client(FakeTransport((200, completion(json.dumps(extraction))))).complete(request_)
    assert json.loads(response.text) == extraction
    assert response.finish_reason == "stop" and response.refusal is None
    assert response.response_source == f"databricks/{ENDPOINT}"
    assert response.raw == {"endpoint": ENDPOINT, "id": "chatcmpl-1", "model": ENDPOINT, "finish_reason": "stop",
                            "usage": {"prompt_tokens": 900, "completion_tokens": 200}}
    assert TOKEN not in json.dumps(response.raw)


def test_content_blocks_are_joined(request_):
    blocks = [{"type": "reasoning", "summary": []}, {"type": "text", "text": '{"a": '}, {"type": "text", "text": "1}"}]
    assert client(FakeTransport((200, completion(blocks)))).complete(request_).text == '{"a": 1}'


@pytest.mark.parametrize("finish, message, expected", [
    ("length", {}, "length"), ("max_tokens", {}, "length"), ("content_filter", {}, "content_filter"),
    ("stop", {"refusal": "I can't help with that."}, "refusal"), ("end_turn", {}, "stop"), (None, {}, "stop")])
def test_finish_reasons_are_mapped(finish, message, expected, request_):
    response = client(FakeTransport((200, completion("{}", finish, **message)))).complete(request_)
    assert response.finish_reason == expected


@pytest.mark.parametrize("reply, error", [
    ((429, {"message": "slow down"}, {"Retry-After": "7"}), ProviderRateLimited),
    ((503, {"message": "overloaded"}), ProviderUnavailable),
    ((500, b"<html>oops</html>"), ProviderUnavailable),
    ((400, {"error_code": "BAD_REQUEST", "message": "temperature not supported"}), ProviderBadRequest),
    ((401, {"message": "invalid token"}), ProviderNotConfigured),
    ((403, {"message": "no permission"}), ProviderNotConfigured),
    ((404, {"message": "endpoint not found"}), ProviderNotConfigured),
    ((200, b"not json"), ProviderUnavailable),
    (socket.timeout("timed out"), ProviderTimeout),
    (TimeoutError(), ProviderTimeout),
    (urllib.error.URLError(socket.timeout("timed out")), ProviderTimeout),
    (urllib.error.URLError("Name or service not known"), ProviderUnavailable),
    (ConnectionResetError("reset"), ProviderUnavailable),
])
def test_http_and_network_failures_are_mapped(reply, error, request_):
    with pytest.raises(error) as caught:
        client(FakeTransport(reply)).complete(request_)
    assert TOKEN not in str(caught.value)
    if error is ProviderRateLimited:
        assert caught.value.retry_after == 7.0
    if error is ProviderBadRequest:
        assert "temperature not supported" in str(caught.value) and caught.value.retryable is False


def test_echoed_credential_in_an_error_body_is_redacted(request_, monkeypatch):
    monkeypatch.setenv("API_KEY", TOKEN)
    with pytest.raises(ProviderBadRequest) as caught:
        client(FakeTransport((400, {"message": f"bad header Authorization: Bearer {TOKEN}"}))).complete(request_)
    assert TOKEN not in str(caught.value)


# Through the pipeline.

def test_pipeline_with_the_databricks_client(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", TOKEN)
    transport = FakeTransport((503, {"message": "warming up"}))       # first call fails, then the retry succeeds
    settings = make_settings(tmp_path, provider="databricks", model_id=ENDPOINT)
    result = Pipeline(settings, DatabricksModelClient(settings, transport=transport, env=ENV), sleep=Sleeps()) \
        .run(ALL_SOURCES)
    assert result.states() == {"done": 21} and len(transport.calls) == 22
    records, queue = read_outputs(tmp_path)
    assert {r["response_source"] for r in records.values()} == {f"databricks/{ENDPOINT}"}
    assert {r["model_id"] for r in records.values()} == {ENDPOINT}
    assert queue["counts"]["Security Review"] == {"review-risk-then-owner": 5}
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8", errors="replace")


def test_switching_provider_refreshes_records(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", TOKEN)
    mock_settings = make_settings(tmp_path)
    Pipeline(mock_settings, MockModelClient(MOCK_RESPONSES), sleep=Sleeps()).run(ALL_SOURCES)
    db_settings = make_settings(tmp_path, provider="databricks", model_id=ENDPOINT)
    transport = FakeTransport()
    result = Pipeline(db_settings, DatabricksModelClient(db_settings, transport=transport, env=ENV),
                      sleep=Sleeps()).run(ALL_SOURCES)
    assert result.model_calls == 21                                   # a provider change is a settings change
    back = Pipeline(mock_settings, MockModelClient(MOCK_RESPONSES), sleep=Sleeps()).run(ALL_SOURCES)
    assert back.states() == {"reused": 21}                           # switching back reuses the mock results
    assert {r["response_source"] for r in read_outputs(tmp_path)[0].values()} == {"mock"}
