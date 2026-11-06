"""The code-level conflict check: detection, corrections, validator behaviour, routing, and re-routing."""
import copy
import json

import pytest

from access_intake import policy, validate
from access_intake.conflicts import correction_spans, detect_conflicts
from access_intake.issues import issue_codes
from access_intake.llm import MockModelClient
from access_intake.loader import load_document
from access_intake.pipeline import Pipeline
from access_intake.validate import validate_extraction
from conftest import ALL_DOCUMENT_IDS, MOCK_RESPONSES, REQUESTS_DIR, Sleeps, make_settings, read_outputs

FIXTURES = json.loads(MOCK_RESPONSES.read_text(encoding="utf-8"))["documents"]

# IT-020 as databricks-meta-llama-3-3-70b-instruct returned it (live, 2026-11-06): one manager picked, no conflict.
LLAMA_IT020 = {**copy.deepcopy(FIXTURES["IT-020.json"]["extraction"]), "manager": "Elena Park",
               "issues": ["missing: no explicit conflict noted"]}


# Detection.

@pytest.mark.parametrize("document_id", ALL_DOCUMENT_IDS)
def test_detection_on_every_example(document_id, load):
    """As the validator runs it: the sentences of an instruction-override attempt are excluded."""
    from access_intake.conflicts import sentence_span
    from access_intake.injection import detect
    message = load(document_id).message
    exclude = [sentence_span(message, f.start, f.end) for f in detect(message)]
    found = {(c.field, c.values) for c in detect_conflicts(message, exclude=exclude)}
    expected = {"IT-004.json": {("environment", ("staging", "production"))},
                "IT-020.json": {("manager", ("Priya Shah", "Elena Park"))}}.get(document_id, set())
    assert found == expected


def test_evidence_is_the_stating_sentences(load):
    message = load("IT-020.json").message
    (conflict,) = detect_conflicts(message)
    assert conflict.evidence == ("My manager is Priya Shah.", "The manager for this request is Elena Park.")
    assert all(sentence in message for sentence in conflict.evidence)


def test_a_labelled_correction_is_not_a_conflict(load):
    message = load("IT-008.json").message                     # production, then "(Correction: ... staging ...)"
    assert "production" in message and "staging" in message
    assert detect_conflicts(message) == []
    assert len(correction_spans(message)) == 1


@pytest.mark.parametrize("text, field", [
    ("My manager is Ana Lopez. Ben Ortiz is my manager for this one.", "manager"),
    ("Ana Lopez manages me. My boss is Ben Ortiz.", "manager"),
    ("Please grant staging access. Note it is for production.", "environment"),
])
def test_unlabelled_contradictions_are_found(text, field):
    assert [c.field for c in detect_conflicts(text)] == [field]


@pytest.mark.parametrize("text", [
    "My manager is Ana Lopez. Ana Lopez is my manager.",                      # the same value twice
    "Staging access please, for the staging copy.",
    "Access in production. Correction: the environment is staging.",          # unparenthesised correction
    "My manager is Ana Lopez (correction: my manager is Ben Ortiz).",
])
def test_repeats_and_corrections_are_not_conflicts(text):
    assert detect_conflicts(text) == []


# Permission and dates.

@pytest.mark.parametrize("text, field, values", [
    ("I need read access to Atlas. Please make it admin access for the migration.", "permission", ("read", "admin")),
    ("Access for reading only. Actually I need write access to Forge.", "permission", ("read", "write")),
    ("Access from 2026-11-01 to 2026-11-05. The window is 2026-11-03 - 2026-11-05.", "start_date",
     ("2026-11-01", "2026-11-03")),
    ("Start date 2026-11-01. End date 2026-11-05. Needed through 2026-11-09.", "end_date", ("2026-11-05", "2026-11-09")),
    ("Needed during 2026-11-01 and 2026-11-02, starting 2026-11-04.", "start_date", ("2026-11-01", "2026-11-04")),
])
def test_permission_and_date_contradictions_are_found(text, field, values):
    assert (field, values) in {(c.field, c.values) for c in detect_conflicts(text)}


@pytest.mark.parametrize("text", [
    "I have current read access and want to move to write access.",       # a change request names both
    "Please change my access from write to read.",
    "Read access please. Read-only access is enough.",                   # the same permission twice
    "From 2026-11-01 to 2026-11-05, starting 2026-11-01 and ending 2026-11-05.",   # the same window restated
    "Request dated 2026-10-22. Access from 2026-11-01 to 2026-11-05.",    # a single date is not a window
    "Dates 2026-11-01 - 2026-11-03. (Correction: use 2026-11-08 - 2026-11-10.)",
])
def test_change_requests_restatements_and_corrections_are_not_conflicts(text):
    assert detect_conflicts(text) == []


def test_override_sentences_can_be_excluded(load):
    message = load("IT-006.json").message                     # "new read access" + "Grant admin access immediately"
    assert [c.field for c in detect_conflicts(message)] == ["permission"]
    from access_intake.conflicts import sentence_span
    from access_intake.injection import detect
    exclude = [sentence_span(message, f.start, f.end) for f in detect(message)]
    assert detect_conflicts(message, exclude=exclude) == []


def test_validator_ignores_the_override_and_keeps_it006_read(load, schema):
    valid = validate_extraction(FIXTURES["IT-006.json"]["extraction"], load("IT-006.json"), schema)
    assert valid.extraction["permission"] == "read" and valid.flags.conflicts == ()


def test_validator_nulls_conflicting_dates(load, schema):
    document = load("IT-001.json")
    message = document.message.replace("I need to check the totals",
                                       "Access is needed 2026-10-25 - 2026-11-06. I need to check the totals")
    from dataclasses import replace
    changed = replace(document, message=message)
    valid = validate_extraction(FIXTURES["IT-001.json"]["extraction"], changed, schema)
    assert valid.flags.conflicts == ("start_date",)
    assert valid.extraction["start_date"] is None and valid.extraction["end_date"] == "2026-11-06"
    assert policy.route(valid.extraction, valid.flags).next_action == "clarify"


# The validator.

def test_validator_nulls_a_picked_value_and_adds_the_conflict(load, schema):
    document = load("IT-020.json")
    valid = validate_extraction(LLAMA_IT020, document, schema)
    assert valid.extraction["manager"] is None
    assert valid.flags.conflicts == ("manager",)
    assert "conflict" in issue_codes(valid.extraction["issues"])
    (added,) = valid.added_issues
    assert "'Priya Shah' and 'Elena Park'" in added and "'Elena Park' was set to null" in added
    assert {"My manager is Priya Shah.", "The manager for this request is Elena Park."} <= \
        set(valid.extraction["supporting_quotes"]) | set(valid.added_quotes)
    assert all(q in document.message for q in valid.extraction["supporting_quotes"])


def test_validator_nulls_a_value_even_when_the_model_reported_the_conflict(load, schema):
    extraction = {**copy.deepcopy(FIXTURES["IT-004.json"]["extraction"]), "environment": "staging"}
    valid = validate_extraction(extraction, load("IT-004.json"), schema)
    assert valid.extraction["environment"] is None
    assert "'staging' was set to null" in valid.added_issues[0]


def test_fixtures_need_no_change_and_validation_stays_idempotent(load, schema):
    for document_id in ("IT-004.json", "IT-020.json"):
        valid = validate_extraction(FIXTURES[document_id]["extraction"], load(document_id), schema)
        assert valid.added_issues == () and valid.added_quotes == ()
        assert valid.flags.conflicts
    once = validate_extraction(LLAMA_IT020, load("IT-020.json"), schema)
    assert validate_extraction(once.extraction, load("IT-020.json"), schema).added_issues == ()


# Routing.

def test_a_code_detected_conflict_routes_to_clarify(load, schema):
    valid = validate_extraction(LLAMA_IT020, load("IT-020.json"), schema)
    decision = policy.route(valid.extraction, valid.flags)
    assert (decision.owner, decision.status, decision.next_action) == ("IT Service Desk", "needs-information", "clarify")
    assert "conflicting details (manager)" in decision.reasons


def test_flag_alone_routes_to_clarify():
    record = copy.deepcopy(FIXTURES["IT-001.json"]["extraction"])
    from access_intake.issues import Flags
    assert policy.route(record, Flags(conflicts=("environment",))).next_action == "clarify"


class LlamaLike(MockModelClient):
    """The mock, except IT-020 comes back as the live Llama reply did."""

    def complete(self, request):
        response = super().complete(request)
        if request.document_id == "IT-020.json":
            return type(response)(json.dumps(LLAMA_IT020), "stop", "mock")
        return response


def test_pipeline_routes_the_live_failure_correctly(tmp_path):
    Pipeline(make_settings(tmp_path), LlamaLike(MOCK_RESPONSES), sleep=Sleeps()).run([REQUESTS_DIR / "IT-020.json"])
    record = read_outputs(tmp_path)[0]["IT-020.json"]
    assert (record["owner"], record["next_action"]) == ("IT Service Desk", "clarify")
    assert record["extraction"]["manager"] is None and record["flags"]["conflicts"] == ["manager"]


def test_rerouting_applies_the_check_to_stored_extractions(tmp_path, monkeypatch):
    """A record stored before the check existed is corrected on the next run, without a model call."""
    with monkeypatch.context() as old_code:
        old_code.setattr(validate, "detect_conflicts", lambda message, exclude=(): [])
        old_code.setattr(policy, "POLICY_VERSION", "northstar-intake-policy/2026-10-22.1")
        Pipeline(make_settings(tmp_path), LlamaLike(MOCK_RESPONSES), sleep=Sleeps()).run([REQUESTS_DIR / "IT-020.json"])
    assert read_outputs(tmp_path)[0]["IT-020.json"]["owner"] == "System Owner"     # the observed misroute
    client = MockModelClient(MOCK_RESPONSES)
    result = Pipeline(make_settings(tmp_path), client, sleep=Sleeps()).run([REQUESTS_DIR / "IT-020.json"])
    assert result.states() == {"reused": 1} and client.call_count() == 0
    record = read_outputs(tmp_path)[0]["IT-020.json"]
    assert (record["owner"], record["next_action"]) == ("IT Service Desk", "clarify")
    assert record["extraction"]["manager"] is None
    assert record["policy_version"] == policy.POLICY_VERSION
