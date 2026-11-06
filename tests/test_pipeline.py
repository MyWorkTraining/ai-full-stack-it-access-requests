"""Operational behaviour: work is visible as it completes, a slow document does not hold up the rest,
and the run manifest records what was done with which settings."""
import json
import threading
import time

from access_intake.llm import MockModelClient
from access_intake.policy import POLICY_VERSION
from access_intake.prompt import PROMPT_VERSION
from conftest import ALL_SOURCES, MOCK_RESPONSES, REQUESTS_DIR, read_outputs


def test_completed_work_is_published_while_the_rest_is_pending(make_pipeline, tmp_path):
    seen = []

    class Watching(MockModelClient):
        def complete(self, request):
            queue = tmp_path / "out" / "queue.json"
            seen.append(json.loads(queue.read_text())["total"] if queue.exists() else 0)
            return super().complete(request)

    make_pipeline(client=Watching(MOCK_RESPONSES), concurrency=1).run(ALL_SOURCES)
    assert seen == list(range(21))          # before each call, every earlier document was already in the queue


def test_a_slow_document_does_not_hold_up_the_others(make_pipeline, tmp_path):
    release = threading.Event()

    class SlowForOne(MockModelClient):
        def complete(self, request):
            if request.document_id == "IT-002.json":
                assert release.wait(timeout=30)
            return super().complete(request)

    pipeline = make_pipeline(client=SlowForOne(MOCK_RESPONSES), concurrency=4)
    worker = threading.Thread(target=pipeline.run, args=([REQUESTS_DIR],))
    worker.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        queue = tmp_path / "out" / "queue.json"
        if queue.exists() and json.loads(queue.read_text())["total"] == 19:
            break
        time.sleep(0.02)
    records, queue = read_outputs(tmp_path)
    assert queue["total"] == 19 and "IT-002.json" not in records   # reviewers can start on 19 of 20
    release.set()
    worker.join(timeout=30)
    assert read_outputs(tmp_path)[1]["total"] == 20


def test_run_manifest_records_inputs_settings_and_outcomes(make_pipeline, monkeypatch):
    monkeypatch.setenv("API_KEY", "sk-test-0000-do-not-leak-0000")
    pipeline = make_pipeline()
    result = pipeline.run(ALL_SOURCES)
    header = pipeline.store.read_run(result.run_id)
    assert header["status"] == "completed" and header["finished_at"]
    assert len(header["inputs"]) == 21
    assert header["prompt_version"] == PROMPT_VERSION and header["policy_version"] == POLICY_VERSION
    assert header["settings"]["model_id"] == "mock-extractor-v1" and header["response_source"] == "mock"
    assert header["counts"] == {"done": 21} and header["model_calls"] == 21
    assert "sk-test-0000" not in json.dumps(header)
    states = pipeline.store.run_states(result.run_id)
    assert len(states) == 21 and {e["state"] for e in states.values()} == {"done"}


def test_progress_callback_sees_every_document(make_pipeline):
    seen = []
    pipeline = make_pipeline()
    pipeline.progress = seen.append
    pipeline.run(ALL_SOURCES)
    assert sorted(o.document_id for o in seen) == sorted(f"IT-{n:03d}.json" for n in [*range(1, 21), 101])


def test_history_records_how_each_attempt_was_made(make_pipeline):
    pipeline = make_pipeline()
    pipeline.run([REQUESTS_DIR / "IT-006.json"])
    (attempt,) = pipeline.store.history("IT-006.json")
    assert attempt["kind"] == "extraction" and attempt["outcome"] == "success"
    assert attempt["model_id"] == "mock-extractor-v1" and attempt["model_params"]["temperature"] == 0.0
    assert attempt["flags"]["injection"] is True
    assert attempt["decision"]["next_action"] == "inspect-message" and attempt["decision"]["policy_rule"] == "1"
    assert attempt["finish_reason"] == "stop"
