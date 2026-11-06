"""The model interface, the mock client's replies and its fault injection, and the provider stub."""
import json
import threading

import pytest

from access_intake.config import Settings
from access_intake.llm import (MockFixtureMissing, MockModelClient, ModelClient, ModelResponse, ProviderBadRequest,
                               ProviderModelClient, ProviderNotConfigured, ProviderRateLimited, ProviderTimeout,
                               ProviderUnavailable, get_client)
from access_intake.llm.mock import UNGROUNDED_QUOTE, Fault, parse_faults
from conftest import ALL_DOCUMENT_IDS, MOCK_RESPONSES, make_request


def test_clients_satisfy_the_interface():
    assert isinstance(MockModelClient(MOCK_RESPONSES), ModelClient)
    assert isinstance(ProviderModelClient(Settings(provider="provider")), ModelClient)


def test_get_client_follows_settings():
    assert isinstance(get_client(Settings(mock_responses=MOCK_RESPONSES)), MockModelClient)
    assert isinstance(get_client(Settings(provider="provider")), ProviderModelClient)


@pytest.mark.parametrize("document_id", ALL_DOCUMENT_IDS)
def test_mock_returns_its_fixture_for_every_example(document_id, mock_client, load):
    request = make_request(load(document_id))
    response = mock_client.complete(request)
    assert response.finish_reason == "stop"
    assert response.response_source == "mock"
    assert response.refusal is None
    fixture = json.loads(MOCK_RESPONSES.read_text())["documents"][document_id]["extraction"]
    assert json.loads(response.text) == fixture


def test_mock_records_requests_and_counts_calls(mock_client, load):
    for document_id in ("IT-001.json", "IT-002.json", "IT-001.json"):
        mock_client.complete(make_request(load(document_id)))
    assert [r.document_id for r in mock_client.requests] == ["IT-001.json", "IT-002.json", "IT-001.json"]
    assert mock_client.call_count("IT-001.json") == 2
    assert mock_client.call_count() == 3


def test_replies_are_independent_copies(mock_client, load):
    request = make_request(load("IT-001.json"))
    first = json.loads(mock_client.complete(request).text)
    first["permission"] = "admin"
    assert json.loads(mock_client.complete(request).text)["permission"] == "read"


def test_changed_source_needs_its_own_fixture(mock_client, load):
    document = load("IT-001.json")
    changed = make_request(document)
    changed = type(changed)(**{**changed.__dict__, "source_sha256": "0" * 64})
    with pytest.raises(MockFixtureMissing, match="changed content"):
        mock_client.complete(changed)
    revised = {**json.loads(mock_client.complete(make_request(document)).text), "end_date": "2026-11-13"}
    mock_client.add_fixture("IT-001.json", "0" * 64, revised)
    assert json.loads(mock_client.complete(changed).text)["end_date"] == "2026-11-13"
    assert json.loads(mock_client.complete(make_request(document)).text)["end_date"] == "2026-11-06"


def test_unknown_document_has_no_fixture(mock_client, load):
    request = make_request(load("IT-001.json"))
    unknown = type(request)(**{**request.__dict__, "document_id": "IT-999.json"})
    with pytest.raises(MockFixtureMissing):
        mock_client.complete(unknown)


@pytest.mark.parametrize("kind, error", [("timeout", ProviderTimeout), ("rate_limit", ProviderRateLimited),
                                         ("unavailable", ProviderUnavailable), ("bad_request", ProviderBadRequest)])
def test_transport_faults_raise_provider_errors(kind, error, load):
    client = MockModelClient(MOCK_RESPONSES, faults=f"IT-002:{kind}")
    with pytest.raises(error) as caught:
        client.complete(make_request(load("IT-002.json")))
    assert caught.value.retryable == (kind != "bad_request")
    assert client.complete(make_request(load("IT-001.json"))).finish_reason == "stop"   # other documents unaffected


def test_rate_limit_carries_retry_after(load):
    client = MockModelClient(MOCK_RESPONSES, faults="IT-002:rate_limit")
    with pytest.raises(ProviderRateLimited) as caught:
        client.complete(make_request(load("IT-002.json")))
    assert caught.value.retry_after == 0.0


def test_malformed_reply_is_not_json(load):
    client = MockModelClient(MOCK_RESPONSES, faults="IT-003:malformed")
    response = client.complete(make_request(load("IT-003.json")))
    assert response.finish_reason == "stop"
    with pytest.raises(json.JSONDecodeError):
        json.loads(response.text)


def test_schema_violation_is_json_of_the_wrong_shape(load, schema):
    import jsonschema
    client = MockModelClient(MOCK_RESPONSES, faults="IT-003:schema_violation")
    reply = json.loads(client.complete(make_request(load("IT-003.json"))).text)
    assert not jsonschema.Draft202012Validator(schema).is_valid(reply)


@pytest.mark.parametrize("kind, finish_reason", [("refusal", "refusal"), ("content_filter", "content_filter"),
                                                 ("truncated", "length")])
def test_unusable_replies_report_their_finish_reason(kind, finish_reason, load):
    client = MockModelClient(MOCK_RESPONSES, faults={"IT-004.json": kind})
    response = client.complete(make_request(load("IT-004.json")))
    assert response.finish_reason == finish_reason
    if kind == "refusal":
        assert response.text is None and response.refusal
    if kind == "truncated":
        with pytest.raises(json.JSONDecodeError):
            json.loads(response.text)


def test_ungrounded_quote_fault_adds_a_quote_not_in_the_message(load):
    document = load("IT-005.json")
    client = MockModelClient(MOCK_RESPONSES, faults="IT-005:ungrounded_quote")
    quotes = json.loads(client.complete(make_request(document)).text)["supporting_quotes"]
    assert UNGROUNDED_QUOTE in quotes and UNGROUNDED_QUOTE not in document.message


def test_fault_limited_to_first_calls_then_succeeds(load):
    client = MockModelClient(MOCK_RESPONSES, faults="IT-002:timeout*2")
    request = make_request(load("IT-002.json"))
    for _ in range(2):
        with pytest.raises(ProviderTimeout):
            client.complete(request)
    assert client.complete(request).finish_reason == "stop"


def test_fail_after_success(load):
    client = MockModelClient(MOCK_RESPONSES, faults="IT-001:fail_after_success")
    request = make_request(load("IT-001.json"))
    assert client.complete(request).finish_reason == "stop"
    with pytest.raises(ProviderUnavailable):
        client.complete(request)


def test_wildcard_fault_and_per_document_override(load):
    client = MockModelClient(MOCK_RESPONSES, faults="*:unavailable,IT-001:refusal")
    with pytest.raises(ProviderUnavailable):
        client.complete(make_request(load("IT-002.json")))
    assert client.complete(make_request(load("IT-001.json"))).finish_reason == "refusal"


def test_parse_faults():
    assert parse_faults("IT-002:timeout*1, IT-003.json:malformed") == {
        "IT-002.json": Fault("timeout", 1), "IT-003.json": Fault("malformed")}
    assert parse_faults("") == {}
    with pytest.raises(ValueError, match="unknown fault"):
        parse_faults("IT-002:explode")
    with pytest.raises(ValueError, match="DOCUMENT:KIND"):
        parse_faults("IT-002")


def test_mock_is_thread_safe(load):
    client = MockModelClient(MOCK_RESPONSES)
    requests = [make_request(load(d)) for d in ALL_DOCUMENT_IDS]
    threads = [threading.Thread(target=lambda r=r: [client.complete(r) for _ in range(5)]) for r in requests]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client.call_count() == 5 * len(requests)


def test_model_response_rejects_unknown_finish_reason():
    with pytest.raises(ValueError):
        ModelResponse("{}", "done", "mock")


def test_request_evidence_holds_no_credential(monkeypatch, load):
    monkeypatch.setenv("API_KEY", "sk-test-0000-do-not-leak")
    evidence = json.dumps(make_request(load("IT-006.json")).evidence())
    assert "sk-test-0000-do-not-leak" not in evidence
    assert "mock-extractor-v1" in evidence


def test_provider_stub_fails_cleanly(monkeypatch, load):
    client = ProviderModelClient(Settings(provider="provider", model_id="some-model"))
    request = make_request(load("IT-001.json"))
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(ProviderNotConfigured, match="API_KEY"):
        client.complete(request)
    monkeypatch.setenv("API_KEY", "sk-test-0000-do-not-leak")
    with pytest.raises(ProviderNotConfigured, match="no model provider") as caught:
        client.complete(request)
    assert "sk-test-0000-do-not-leak" not in str(caught.value)
    assert caught.value.retryable is False
