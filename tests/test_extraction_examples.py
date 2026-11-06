"""Plan section 7.1: the whole pipeline, with the mock model, over all 21 example messages.

One run processes the 20 supplied exports and the paraphrase IT-101 into a temporary state; each
document's published record is then checked against the independently written expected truth.
"""
import hashlib

import jsonschema
import pytest

from access_intake.issues import issue_codes
from access_intake.llm import MockModelClient
from access_intake.pipeline import Pipeline
from access_intake.policy import POLICY_VERSION
from access_intake.prompt import PROMPT_VERSION
from conftest import (ALL_DOCUMENT_IDS, ALL_SOURCES, EXPECTED_DOCUMENTS, MOCK_RESPONSES, SCALAR_FIELDS, SUPPLIED_IDS,
                      Sleeps, make_settings, read_outputs, source_path)


@pytest.fixture(scope="module")
def batch(tmp_path_factory):
    base = tmp_path_factory.mktemp("batch")
    client = MockModelClient(MOCK_RESPONSES)
    result = Pipeline(make_settings(base), client, sleep=Sleeps()).run(ALL_SOURCES)
    records, queue = read_outputs(base)
    return result, records, queue, client, base


@pytest.fixture(params=ALL_DOCUMENT_IDS)
def record(request, batch):
    _, records, _, _, _ = batch
    return request.param, records[request.param], EXPECTED_DOCUMENTS[request.param]


def test_record_is_a_successful_mock_extraction(record, schema):
    document_id, published, _ = record
    assert published["status"] != "error" and published["error"] is None
    assert published["response_source"] == "mock"
    jsonschema.Draft202012Validator(schema).validate(published["extraction"])


def test_record_identity_and_provenance(record):
    document_id, published, _ = record
    path = source_path(document_id)
    assert published["document_id"] == document_id == path.name
    assert published["source_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert published["prompt_version"] == PROMPT_VERSION
    assert published["policy_version"] == POLICY_VERSION
    assert published["settings_fingerprint"] and published["attempt_id"]


def test_record_scalars_match_expected(record):
    _, published, expected = record
    assert {name: published["extraction"][name] for name in SCALAR_FIELDS} == expected["fields"]


def test_record_quotes_are_grounded_and_complete(record, load):
    document_id, published, expected = record
    message = load(document_id).message
    quotes = published["extraction"]["supporting_quotes"]
    assert all(q in message for q in quotes)
    for required in expected["required_quotes"]:
        assert any(required in q for q in quotes), required


def test_record_issue_codes_match_expected(record):
    _, published, expected = record
    assert issue_codes(published["extraction"]["issues"]) == set(expected["issue_codes"])


def test_record_route_matches_expected(record):
    _, published, expected = record
    route = expected["route"]
    assert (published["owner"], published["status"], published["next_action"]) == \
        (route["owner"], route["status"], route["next_action"])


def test_run_completed_with_one_model_call_per_document(batch):
    result, _, _, client, _ = batch
    assert result.status == "completed"
    assert result.states() == {"done": 21}
    assert client.call_count() == 21 == result.model_calls


def test_queue_totals(batch):
    _, records, queue, _, _ = batch
    assert queue["total"] == 21
    assert queue["counts"] == {
        "System Owner": {"review-request": 5},            # IT-001, 008, 015, 018 and the paraphrase IT-101
        "Security Review": {"review-risk-then-owner": 5},
        "IT Service Desk": {"clarify": 9, "inspect-message": 1, "confirm-withdrawal": 1},
    }
    supplied = [e for e in queue["entries"] if e["document_id"] in SUPPLIED_IDS]
    assert sum(e["owner"] == "System Owner" for e in supplied) == 4


def test_queue_and_extractions_agree_on_identity_and_source(batch):
    _, records, queue, _, _ = batch
    assert {(e["document_id"], e["source_sha256"]) for e in queue["entries"]} == \
        {(r["document_id"], r["source_sha256"]) for r in records.values()}
    for entry in queue["entries"]:
        record_ = records[entry["document_id"]]
        assert (entry["owner"], entry["status"], entry["next_action"]) == \
            (record_["owner"], record_["status"], record_["next_action"])


def test_records_have_the_required_deliverable_fields(batch):
    _, records, _, _, _ = batch
    required = {"document_id", "source_sha256", "extraction", "status", "owner", "next_action", "error",
                "response_source"}
    for published in records.values():
        assert required <= set(published)


def test_model_saw_the_real_prompt_for_each_document(batch):
    _, _, _, client, _ = batch
    assert sorted(r.document_id for r in client.requests) == ALL_DOCUMENT_IDS
    for request in client.requests:
        assert request.system_prompt.startswith("You extract facts")
        assert f"Filename: {request.document_id}" in request.user_content
