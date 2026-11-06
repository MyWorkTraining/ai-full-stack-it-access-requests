"""Reprocessing: unchanged reruns, changed sources and settings, retries, interruption, two operators."""
import copy
import hashlib
import json
import shutil
import threading
import time

import pytest

from access_intake import policy
from access_intake.llm import MockModelClient
from access_intake.store import Store
from conftest import ALL_SOURCES, MOCK_RESPONSES, REQUESTS_DIR, history_files, make_settings, read_outputs

FIXTURES = json.loads(MOCK_RESPONSES.read_text(encoding="utf-8"))["documents"]


def outputs_text(base):
    return (base / "out" / "extractions.jsonl").read_text(), (base / "out" / "queue.json").read_text()


# Unchanged and changed inputs.

def test_unchanged_rerun_makes_no_model_calls_and_changes_nothing(make_pipeline, tmp_path):
    client = MockModelClient(MOCK_RESPONSES)
    make_pipeline(client=client).run(ALL_SOURCES)
    before, history_before = outputs_text(tmp_path), history_files(tmp_path)
    again = make_pipeline(client=client).run(ALL_SOURCES)
    assert again.states() == {"skipped": 21} and again.model_calls == 0
    assert client.call_count() == 21
    assert outputs_text(tmp_path) == before
    assert history_files(tmp_path) == history_before
    records, queue = read_outputs(tmp_path)
    assert len(records) == queue["total"] == 21


@pytest.fixture
def batch_copy(tmp_path):
    """A writable copy of the supplied exports."""
    folder = tmp_path / "exports"
    shutil.copytree(REQUESTS_DIR, folder)
    return folder


def edit_it001(folder):
    """Re-export IT-001 with a corrected end date; returns (old sha, new sha, the matching extraction)."""
    path = folder / "IT-001.json"
    old = hashlib.sha256(path.read_bytes()).hexdigest()
    data = json.loads(path.read_bytes())
    data["message"] = data["message"].replace("2026-11-06", "2026-11-13")
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    new = hashlib.sha256(path.read_bytes()).hexdigest()
    extraction = copy.deepcopy(FIXTURES["IT-001.json"]["extraction"])
    extraction["end_date"] = "2026-11-13"
    extraction["supporting_quotes"] = [q.replace("2026-11-06", "2026-11-13") for q in extraction["supporting_quotes"]]
    return old, new, extraction


def test_changed_source_creates_a_new_result_and_keeps_the_old(make_pipeline, tmp_path, batch_copy):
    client = MockModelClient(MOCK_RESPONSES)
    pipeline = make_pipeline(client=client)
    pipeline.run([batch_copy])
    old, new, extraction = edit_it001(batch_copy)
    client.add_fixture("IT-001.json", new, extraction)
    rerun = make_pipeline(client=client).run([batch_copy])
    assert rerun.outcomes["IT-001.json"].state == "done" and rerun.model_calls == 1
    assert rerun.states() == {"done": 1, "skipped": 19}
    records, queue = read_outputs(tmp_path)
    assert records["IT-001.json"]["source_sha256"] == new
    assert records["IT-001.json"]["extraction"]["end_date"] == "2026-11-13"
    assert queue["total"] == 20                                       # still one entry per document
    hashes = [a["source_sha256"] for a in pipeline.store.history("IT-001.json")]
    assert hashes == [old, new]                                       # earlier result kept, with its hash
    assert pipeline.store.history("IT-001.json")[0]["extraction"]["end_date"] == "2026-11-06"


def test_changed_source_that_fails_shows_the_error_and_the_earlier_result(make_pipeline, tmp_path, batch_copy):
    client = MockModelClient(MOCK_RESPONSES)
    make_pipeline(client=client).run([batch_copy])
    old, _, _ = edit_it001(batch_copy)                                # no fixture for the new content
    make_pipeline(client=client).run([batch_copy])
    record = read_outputs(tmp_path)[0]["IT-001.json"]
    assert (record["status"], record["next_action"]) == ("error", "retry-extraction")
    assert record["error"]["code"] == "mock-fixture-missing"
    assert record["last_valid_attempt"]["source_sha256"] == old
    assert record["last_valid_attempt"]["same_source"] is False


def test_failed_retry_after_success_keeps_the_last_valid_result(make_pipeline, tmp_path):
    client = MockModelClient(MOCK_RESPONSES, faults="IT-001:fail_after_success")
    first = make_pipeline(client=client, max_retries=0).run(ALL_SOURCES)
    success_id = first.outcomes["IT-001.json"].attempt_id
    success_file = next(p for p in history_files(tmp_path) if p.stem == success_id)
    success_bytes = success_file.read_bytes()

    second = make_pipeline(client=client, max_retries=0, model_id="mock-extractor-v2").run(ALL_SOURCES)
    assert second.outcomes["IT-001.json"].state == "error"
    assert second.states() == {"done": 20, "error": 1}
    records, queue = read_outputs(tmp_path)
    record = records["IT-001.json"]
    assert record["status"] == "error" and record["error"]["code"] == "provider-unavailable"
    assert record["model_id"] == "mock-extractor-v2"
    assert record["last_valid_attempt"]["attempt_id"] == success_id
    assert record["last_valid_attempt"]["model_id"] == "mock-extractor-v1"
    assert record["last_valid_attempt"]["same_source"] is True
    assert record["last_valid_attempt"]["settings_fingerprint"] != record["settings_fingerprint"]
    assert success_file.read_bytes() == success_bytes               # history is never rewritten
    entry = next(e for e in queue["entries"] if e["document_id"] == "IT-001.json")
    assert (entry["status"], entry["next_action"]) == ("error", "retry-extraction")


# Settings, policy and prompt changes.

def test_model_change_refreshes_every_record(make_pipeline, tmp_path):
    first = make_pipeline().run(ALL_SOURCES)
    before = {d: r["settings_fingerprint"] for d, r in read_outputs(tmp_path)[0].items()}
    second = make_pipeline(model_id="mock-extractor-v2").run(ALL_SOURCES)
    assert second.states() == {"done": 21} and second.model_calls == 21
    after = read_outputs(tmp_path)[0]
    assert all(after[d]["settings_fingerprint"] != fp for d, fp in before.items())
    assert all(r["model_id"] == "mock-extractor-v2" for r in after.values())
    assert len(history_files(tmp_path)) == 42
    assert first.model_calls == 21


def test_returning_to_earlier_settings_reuses_stored_extractions(make_pipeline, tmp_path):
    make_pipeline().run(ALL_SOURCES)
    make_pipeline(model_id="mock-extractor-v2").run(ALL_SOURCES)
    back = make_pipeline().run(ALL_SOURCES)
    assert back.states() == {"reused": 21} and back.model_calls == 0
    assert all(r["model_id"] == "mock-extractor-v1" for r in read_outputs(tmp_path)[0].values())


def test_policy_change_reroutes_without_model_calls(make_pipeline, tmp_path, monkeypatch):
    client = MockModelClient(MOCK_RESPONSES)
    make_pipeline(client=client).run(ALL_SOURCES)
    monkeypatch.setattr(policy, "POLICY_VERSION", "northstar-intake-policy/test-2")
    rerouted = make_pipeline(client=client).run(ALL_SOURCES)
    assert rerouted.states() == {"reused": 21} and client.call_count() == 21
    records, queue = read_outputs(tmp_path)
    assert queue["policy_versions"] == ["northstar-intake-policy/test-2"]
    assert all(r["policy_version"] == "northstar-intake-policy/test-2" for r in records.values())
    assert all(r["extraction_attempt_id"] != r["attempt_id"] for r in records.values())   # points at the reused one
    assert queue["counts"]["System Owner"] == {"review-request": 5}


def test_prompt_change_refreshes_records(make_pipeline, monkeypatch):
    make_pipeline().run(ALL_SOURCES)
    monkeypatch.setattr("access_intake.pipeline.PROMPT_VERSION", "access-extract-1+edited")
    assert make_pipeline().run(ALL_SOURCES).model_calls == 21


def test_force_reprocesses_everything(make_pipeline):
    make_pipeline().run(ALL_SOURCES)
    assert make_pipeline().run(ALL_SOURCES, force=True).model_calls == 21


# Retry.

def test_retry_reprocesses_a_failed_document_only(make_pipeline, tmp_path):
    make_pipeline(faults="IT-002:unavailable", max_retries=0).run(ALL_SOURCES)
    retried = make_pipeline().retry(["IT-002"])
    assert retried.states() == {"done": 1} and retried.model_calls == 1
    assert read_outputs(tmp_path)[0]["IT-002.json"]["owner"] == "Security Review"


def test_retry_errors_and_unknown_documents(make_pipeline):
    make_pipeline(faults="IT-002:unavailable,IT-003:malformed", max_retries=0).run(ALL_SOURCES)
    assert make_pipeline().retry(errors=True).states() == {"done": 2}
    assert make_pipeline().retry(errors=True).message == "nothing to retry"
    with pytest.raises(KeyError, match="IT-999.json"):
        make_pipeline().retry(["IT-999.json"])


# Interruption and resume.

class Interrupt(BaseException):
    """Stands in for a deployment stop or Ctrl-C in the middle of a run."""


class InterruptingClient(MockModelClient):
    def __init__(self, at: int) -> None:
        super().__init__(MOCK_RESPONSES)
        self.at = at

    def complete(self, request):
        if len(self.requests) + 1 == self.at:
            self.requests.append(request)
            raise Interrupt()
        return super().complete(request)


def test_interrupted_run_resumes_without_redoing_completed_work(make_pipeline, tmp_path):
    pipeline = make_pipeline(client=InterruptingClient(at=6), concurrency=1)
    with pytest.raises(Interrupt):
        pipeline.run(ALL_SOURCES)
    run = pipeline.store.runs()[-1]
    assert run["status"] == "interrupted" and "intake resume" in run["message"]
    states = [e["state"] for e in pipeline.store.run_states(run["run_id"]).values()]
    assert sorted(set(states)) == ["done", "pending", "running"]
    assert states.count("done") == 5 and states.count("running") == 1 and states.count("pending") == 15
    assert len(read_outputs(tmp_path)[0]) == 5                      # completed work is already published

    client = MockModelClient(MOCK_RESPONSES)
    resumed = make_pipeline(client=client).resume()
    assert resumed.run_id == run["run_id"] and resumed.status == "completed"
    assert client.call_count() == 16                                 # the 5 done documents were not re-called
    assert {r.document_id for r in client.requests}.isdisjoint({f"IT-00{n}.json" for n in range(1, 6)})
    assert len(read_outputs(tmp_path)[0]) == 21
    assert pipeline.store.read_run(run["run_id"])["status"] == "completed"


# Operators working at the same time.

def test_two_operators_at_once_process_each_document_once(tmp_path, sleeps):
    from access_intake.pipeline import Pipeline
    clients = [MockModelClient(MOCK_RESPONSES), MockModelClient(MOCK_RESPONSES)]
    pipelines = [Pipeline(make_settings(tmp_path), client, Store(tmp_path / "state", tmp_path / "out"), sleep=sleeps)
                 for client in clients]
    barrier, results = threading.Barrier(2), [None, None]

    def operate(i):
        barrier.wait()
        results[i] = pipelines[i].run(ALL_SOURCES)

    threads = [threading.Thread(target=operate, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sum(c.call_count() for c in clients) == 21              # no document extracted twice
    for document_id in {r.document_id for c in clients for r in c.requests}:
        attempts = pipelines[0].store.history(document_id)
        assert [a["kind"] for a in attempts] == ["extraction"]
    records, queue = read_outputs(tmp_path)
    assert len(records) == queue["total"] == 21
    for result in results:
        assert set(result.states()) <= {"done", "skipped", "deferred"}


def test_live_claim_defers_and_stale_claim_is_taken_over(make_pipeline, tmp_path):
    pipeline = make_pipeline()
    claims = tmp_path / "state" / "claims"
    (claims / "IT-001.json").write_text(json.dumps({"run_id": "other", "claimed_at": time.time()}))
    (claims / "IT-002.json").write_text(json.dumps({"run_id": "other", "claimed_at": time.time() - 10_000}))
    result = pipeline.run([REQUESTS_DIR / "IT-001.json", REQUESTS_DIR / "IT-002.json"])
    assert result.outcomes["IT-001.json"].state == "deferred"
    assert result.outcomes["IT-002.json"].state == "done"
    assert result.status == "incomplete"
    (claims / "IT-001.json").unlink()                                 # the other operator finished or gave up
    assert make_pipeline().resume().states() == {"done": 1}


# Store guarantees.

def test_no_temporary_files_are_left_behind(make_pipeline, tmp_path):
    make_pipeline(faults="IT-002:unavailable", max_retries=0).run(ALL_SOURCES)
    assert not list(tmp_path.rglob("*.tmp"))


def test_history_entries_are_immutable(make_pipeline):
    pipeline = make_pipeline()
    result = pipeline.run([REQUESTS_DIR / "IT-001.json"])
    attempt = pipeline.store.history("IT-001.json")[0]
    assert attempt["attempt_id"] == result.outcomes["IT-001.json"].attempt_id
    with pytest.raises(FileExistsError):
        pipeline.store.commit(attempt, pipeline.store.read_current("IT-001.json"))


def test_duplicate_document_ids_are_rejected(make_pipeline, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    shutil.copy(REQUESTS_DIR / "IT-001.json", other / "IT-001.json")
    with pytest.raises(ValueError, match="IT-001.json"):
        make_pipeline().run([REQUESTS_DIR / "IT-001.json", other])
