"""The accuracy check (counting rules, labelling) and the request-evidence file."""
import copy
import json

import pytest

from access_intake.accuracy import compare, render_markdown, run_accuracy
from access_intake.cli import EXIT_OK, main
from access_intake.evidence import build_evidence
from access_intake.llm import MockModelClient
from conftest import EXPECTED_DOCUMENTS, EXPECTED_PATH, MOCK_RESPONSES, REQUESTS_DIR, ROOT, Sleeps, make_settings

FIXTURES = json.loads(MOCK_RESPONSES.read_text(encoding="utf-8"))["documents"]
FAKE_KEY = "sk-test-0000-do-not-leak-0000"


def perfect_records():
    """Records as the pipeline would publish them for the mock fixtures."""
    records = {}
    for document_id, entry in FIXTURES.items():
        route = EXPECTED_DOCUMENTS[document_id]["route"]
        records[document_id] = {"document_id": document_id, "extraction": copy.deepcopy(entry["extraction"]),
                                "response_source": "mock", "error": None, **route}
    return records


# Counting rules.

def test_perfect_records_score_231_of_231():
    report = compare(perfect_records(), EXPECTED_DOCUMENTS, ROOT)
    assert report.scalar_total == 231 and report.scalar("match") == 231
    assert report.mismatches == [] and report.failed == []
    assert report.purpose["pass"] == report.quotes["pass"] == report.issues["pass"] == report.routes["pass"] == 21


def test_omissions_factual_errors_and_ungrounded_values_are_distinguished():
    records = perfect_records()
    records["IT-001.json"]["extraction"]["manager"] = None                 # omission
    records["IT-004.json"]["extraction"]["environment"] = "staging"        # value where truth is null
    records["IT-002.json"]["extraction"]["requester"] = "Olivia Reed"      # value not in the message
    records["IT-020.json"]["extraction"]["manager"] = "Elena Park"         # picks one side of a conflict
    report = compare(records, EXPECTED_DOCUMENTS, ROOT)
    assert report.scalar("match") == 227
    assert report.scalar("omission") == 1 and report.scalar("factual-error") == 3
    errors = {(m.document_id, m.check): m for m in report.factual_errors()}
    assert errors[("IT-002.json", "requester")].note == "not in message"
    assert errors[("IT-020.json", "manager")].note == ""                  # in the message, but wrong here
    assert ("IT-004.json", "environment") in errors


def test_failed_extraction_scores_no_scalar_and_is_listed():
    records = perfect_records()
    records["IT-002.json"] = {"document_id": "IT-002.json", "extraction": None, "response_source": "mock",
                              "error": {"code": "provider-unavailable"}, "owner": "IT Service Desk",
                              "status": "error", "next_action": "retry-extraction"}
    report = compare(records, EXPECTED_DOCUMENTS, ROOT)
    assert report.failed == ["IT-002.json"]
    assert report.scalar("failed") == 11 and report.scalar("match") == 220
    assert report.routes["fail"] == 1
    assert any(m.check == "extraction" and m.note == "provider-unavailable" for m in report.mismatches)


def test_purpose_quotes_and_issue_checks_fail_independently():
    records = perfect_records()
    records["IT-001.json"]["extraction"]["business_purpose"] = "Needs access"
    records["IT-016.json"]["extraction"]["issues"] = []
    records["IT-004.json"]["extraction"]["supporting_quotes"] = records["IT-004.json"]["extraction"]["supporting_quotes"][:2]
    report = compare(records, EXPECTED_DOCUMENTS, ROOT)
    assert (report.purpose["fail"], report.issues["fail"], report.quotes["fail"]) == (1, 1, 1)
    assert report.scalar("match") == 231


# The full check and its report.

def test_accuracy_with_the_mock_is_perfect_and_labelled_as_mocked(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    settings = make_settings(tmp_path)
    out = tmp_path / "accuracy.md"
    report, text = run_accuracy(settings, EXPECTED_PATH, out, client=MockModelClient(MOCK_RESPONSES), sleep=Sleeps())
    assert report.scalar("match") == 231 and report.mismatches == []
    assert report.response_sources == ["mock"]
    assert out.read_text() == text
    for snippet in ("| Scalar fields, exact match | 231 / 231 |", "MOCKED responses", "Live provider observations: none",
                    "Denominator: 11 x 21 = 231", "| IT-101.json (paraphrase) |", "| IT-016.json | IT-061 |",
                    "### Factual errors\n\nNone."):
        assert snippet in text
    assert not (tmp_path / "out").exists()                             # the review queue was not touched


def test_render_labels_live_results():
    report = compare({d: {**r, "response_source": "openai/gpt-x"} for d, r in perfect_records().items()},
                     EXPECTED_DOCUMENTS, ROOT)
    meta = {"generated_at": "now", "provider": "provider", "model_id": "gpt-x", "params": {}, "prompt_version": "p",
            "policy_version": "v", "settings_fingerprint": "f", "expected_path": "e"}
    text = render_markdown(report, EXPECTED_DOCUMENTS, meta)
    assert "**openai/gpt-x** (live provider responses)" in text and "MOCKED" not in text


# Request evidence.

def test_evidence_shows_the_request_configuration_and_handling(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_KEY)
    settings = make_settings(tmp_path)
    evidence = build_evidence(settings, REQUESTS_DIR / "IT-006.json", MockModelClient(MOCK_RESPONSES))
    text = json.dumps(evidence)
    assert FAKE_KEY not in text
    assert evidence["credential_handling"]["http_headers_as_sent"]["Authorization"] == "Bearer [REDACTED]"
    assert evidence["configuration"]["model_id"] == "mock-extractor-v1"
    assert evidence["configuration"]["model_params"] == {"temperature": 0.0, "max_output_tokens": 2000, "timeout_s": 60.0}
    assert evidence["request"]["messages"][0]["role"] == "system"
    assert "SYSTEM OVERRIDE" in evidence["request"]["messages"][1]["content"]
    assert evidence["request"]["response_format"]["schema"]["additionalProperties"] is False
    assert evidence["example_response"]["finish_reason"] == "stop"
    assert evidence["handled_outcome"]["route"]["next_action"] == "inspect-message"
    codes = {c for stage in evidence["response_handling"] for c in stage["error_codes"]}
    assert {"refused", "truncated", "malformed-output", "schema-violation", "provider-unavailable"} <= codes


def test_evidence_records_a_rejected_reply(tmp_path):
    evidence = build_evidence(make_settings(tmp_path), REQUESTS_DIR / "IT-001.json",
                              MockModelClient(MOCK_RESPONSES, faults="IT-001:truncated"))
    assert evidence["handled_outcome"]["accepted"] is False
    assert evidence["handled_outcome"]["error"]["code"] == "truncated"


# CLI.

@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("MOCK_FAULTS", raising=False)

    def run(*args):
        code = main(["--state-dir", str(tmp_path / "state"), "--output-dir", str(tmp_path / "out"), *args])
        return code, capsys.readouterr().out
    return run


def test_cli_accuracy_and_evidence(cli, tmp_path):
    code, out = cli("accuracy", "--out", str(tmp_path / "accuracy.md"))
    assert code == EXIT_OK and "Scalar fields exact match: 231 / 231" in out and "Response source: mock" in out
    assert (tmp_path / "accuracy.md").is_file()
    code, out = cli("evidence", "--out", str(tmp_path / "evidence.json"))
    assert code == EXIT_OK and "IT Service Desk / review / inspect-message" in out
    assert json.loads((tmp_path / "evidence.json").read_text())["generated_by"].endswith("IT-006.json")
