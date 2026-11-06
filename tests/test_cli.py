"""The intake command line: commands, output an operator can act on, and exit codes."""
import json
import os
import subprocess
import sys

import pytest

from access_intake.cli import EXIT_ERRORS, EXIT_OK, EXIT_UNFINISHED, EXIT_USAGE, main
from conftest import ROOT

FAKE_KEY = "sk-test-0000-do-not-leak-0000"


@pytest.fixture
def intake(tmp_path, monkeypatch, capsys):
    """Run `intake <args>` from the repository root against state and outputs in tmp_path."""
    monkeypatch.chdir(ROOT)
    for name in ("MOCK_FAULTS", "INTAKE_PROVIDER", "INTAKE_MODEL_ID", "INTAKE_STATE_DIR", "INTAKE_OUTPUT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INTAKE_BACKOFF_BASE_S", "0")
    monkeypatch.setenv("INTAKE_BACKOFF_MAX_S", "0")

    def run(*args):
        code = main(["--state-dir", str(tmp_path / "state"), "--output-dir", str(tmp_path / "out"), *args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    return run


def test_run_processes_the_batch_and_prints_each_document(intake):
    code, out, _ = intake("run", "materials/requests")
    assert code == EXIT_OK
    assert sum(line.startswith("IT-") for line in out.splitlines()) == 20
    assert "IT-006.json      done      IT Service Desk / review / inspect-message" in out
    assert ": completed (20 done; 20 model call(s))." in out
    assert "Processing with mock" in out


def test_rerun_skips_unchanged_documents(intake):
    intake("run", "materials/requests")
    code, out, _ = intake("run", "materials/requests")
    assert code == EXIT_OK and "(20 skipped; 0 model call(s))" in out


def test_status_shows_run_progress_and_queue_counts(intake):
    intake("run", "materials/requests")
    code, out, _ = intake("status")
    assert code == EXIT_OK
    assert ": completed" in out and "documents: 20 done" in out
    assert "Current queue: 20 record(s)" in out
    for line in ("IT Service Desk  clarify                  9", "Security Review  review-risk-then-owner   5",
                 "System Owner     review-request           4"):
        assert line in out


def test_status_before_any_run(intake):
    code, out, _ = intake("status")
    assert code == EXIT_OK and "No runs yet" in out


def test_queue_filters_and_json(intake):
    intake("run", "materials/requests")
    code, out, _ = intake("queue", "--owner", "security review")
    assert code == EXIT_OK and "5 of 20 entries" in out
    _, out, _ = intake("queue", "--next-action", "clarify", "--json")
    entries = json.loads(out)
    assert sorted(e["document_id"] for e in entries) == [
        "IT-003.json", "IT-004.json", "IT-009.json", "IT-010.json", "IT-011.json", "IT-014.json", "IT-016.json",
        "IT-017.json", "IT-020.json"]
    _, out, _ = intake("queue")
    assert "IT-016.json    IT-061" in out                        # the stated ID beside the document ID


def test_queue_before_any_run(intake):
    code, out, _ = intake("queue")
    assert code == EXIT_OK and "No queue yet" in out


def test_show_a_record_and_its_history(intake):
    intake("run", "materials/requests")
    code, out, _ = intake("show", "IT-016", "--history")
    assert code == EXIT_OK
    assert "request_id        IT-061" in out
    assert "id-mismatch:" in out and "IT Service Desk / needs-information / clarify" in out
    assert "history (1 attempt(s)" in out
    _, out, _ = intake("show", "IT-004.json", "--json")
    record = json.loads(out)["record"]
    assert record["extraction"]["environment"] is None
    code, _, err = intake("show", "IT-999")
    assert code == EXIT_USAGE and "No current record" in err


def test_failure_is_reported_with_its_retry_command_and_retry_fixes_it(intake, monkeypatch):
    monkeypatch.setenv("MOCK_FAULTS", "IT-002:unavailable")
    code, out, _ = intake("run", "materials/requests")
    assert code == EXIT_ERRORS
    assert "IT-002.json      error     IT Service Desk / error / retry-extraction  [provider-unavailable] " \
           "retry: intake retry IT-002.json" in out
    _, out, _ = intake("status")
    assert "IT-002.json      provider-unavailable     intake retry IT-002.json" in out
    _, out, _ = intake("show", "IT-002.json")
    assert "retry: intake retry IT-002.json" in out
    monkeypatch.delenv("MOCK_FAULTS")
    code, out, _ = intake("retry", "IT-002.json")
    assert code == EXIT_OK and "IT-002.json      done      Security Review" in out


def test_retry_needs_a_target(intake):
    code, _, err = intake("retry")
    assert code == EXIT_USAGE and "--errors" in err
    code, _, err = intake("retry", "IT-002.json")
    assert code == EXIT_USAGE and "no current record" in err


def test_outage_halts_then_resume_and_retry_finish_the_intake(intake, monkeypatch):
    monkeypatch.setenv("MOCK_FAULTS", "*:unavailable")
    monkeypatch.setenv("INTAKE_MAX_RETRIES", "0")
    monkeypatch.setenv("INTAKE_BREAKER_THRESHOLD", "2")
    code, out, _ = intake("--concurrency", "1", "run", "materials/requests")
    assert code == EXIT_UNFINISHED and ": halted" in out and "intake resume run-" in out
    _, out, _ = intake("status")
    assert "open (18)" in out and "continue with: intake resume" in out
    monkeypatch.delenv("MOCK_FAULTS")
    code, out, _ = intake("resume")
    assert code == EXIT_ERRORS and "18 done" in out                 # the 2 earlier errors remain
    code, out, _ = intake("retry", "--errors")
    assert code == EXIT_OK and "(2 done;" in out
    code, out, _ = intake("resume")
    assert code == EXIT_OK and "Nothing to resume" in out


def test_unconfigured_provider_explains_itself(intake, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    code, out, _ = intake("--provider", "provider", "--concurrency", "1", "run", "materials/requests")
    assert code == EXIT_UNFINISHED and "[provider-not-configured]" in out
    _, out, _ = intake("show", "IT-001.json")
    assert "API_KEY is not set" in out


def test_missing_input_path_is_a_usage_error(intake):
    code, _, err = intake("run", "materials/no-such-folder")
    assert code == EXIT_USAGE and "no such file or directory" in err


def test_verbose_logs_are_redacted(intake, monkeypatch):
    monkeypatch.setenv("API_KEY", FAKE_KEY)
    code, _, err = intake("-v", "run", "materials/requests/IT-001.json")
    assert code == EXIT_OK and "IT-001.json: done" in err and FAKE_KEY not in err


def test_module_entry_point():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    done = subprocess.run([sys.executable, "-m", "access_intake", "--help"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "resume" in done.stdout
