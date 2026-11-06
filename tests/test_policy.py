"""The review-queue policy: every example's placement, each rule, and rule precedence."""
import copy
import json
from collections import Counter

import pytest

from access_intake.issues import Flags
from access_intake.policy import (POLICY_VERSION, REQUIRED_FACTS, SECURITY_REVIEW, SERVICE_DESK, SYSTEM_OWNER,
                                  missing_facts, route, route_error)
from access_intake.validate import validate_response
from conftest import ALL_DOCUMENT_IDS, EXPECTED_DOCUMENTS, MOCK_RESPONSES, SUPPLIED_IDS, make_request

FIXTURES = json.loads(MOCK_RESPONSES.read_text(encoding="utf-8"))["documents"]
ALLOWED = {(SERVICE_DESK, "review", "inspect-message"), (SERVICE_DESK, "review", "confirm-withdrawal"),
           (SERVICE_DESK, "needs-information", "clarify"), (SECURITY_REVIEW, "review", "review-risk-then-owner"),
           (SYSTEM_OWNER, "review", "review-request"), (SERVICE_DESK, "error", "retry-extraction")}

# A complete staging/read new request (IT-001), the base for table-driven cases.
BASE = copy.deepcopy(FIXTURES["IT-001.json"]["extraction"])


def record(**changes):
    return {**copy.deepcopy(BASE), **changes}


def placement(decision):
    return decision.owner, decision.status, decision.next_action


# Every example, end to end through the mock model and the validator.

@pytest.mark.parametrize("document_id", ALL_DOCUMENT_IDS)
def test_every_example_routes_as_expected(document_id, mock_client, load, schema):
    document = load(document_id)
    valid = validate_response(mock_client.complete(make_request(document)), document, schema)
    decision = route(valid.extraction, valid.flags)
    expected = EXPECTED_DOCUMENTS[document_id]["route"]
    assert placement(decision) == (expected["owner"], expected["status"], expected["next_action"])
    assert decision.reasons


def test_queue_totals_for_the_supplied_batch(mock_client, load, schema):
    totals = Counter()
    for document_id in SUPPLIED_IDS:
        document = load(document_id)
        valid = validate_response(mock_client.complete(make_request(document)), document, schema)
        decision = route(valid.extraction, valid.flags)
        totals[(decision.owner, decision.next_action)] += 1
    assert totals == {(SYSTEM_OWNER, "review-request"): 4, (SECURITY_REVIEW, "review-risk-then-owner"): 5,
                      (SERVICE_DESK, "clarify"): 9, (SERVICE_DESK, "inspect-message"): 1,
                      (SERVICE_DESK, "confirm-withdrawal"): 1}


# Rules 4 and 5.

@pytest.mark.parametrize("environment, permission, owner, rule", [
    ("staging", "read", SYSTEM_OWNER, "5"),
    ("staging", "write", SECURITY_REVIEW, "4"),
    ("staging", "admin", SECURITY_REVIEW, "4"),
    ("production", "read", SECURITY_REVIEW, "4"),
    ("production", "write", SECURITY_REVIEW, "4"),
    ("production", "admin", SECURITY_REVIEW, "4"),
])
@pytest.mark.parametrize("kind", ["new", "change"])
def test_complete_requests_by_risk(kind, environment, permission, owner, rule):
    decision = route(record(kind=kind, environment=environment, permission=permission))
    assert (decision.owner, decision.status, decision.rule) == (owner, "review", rule)


# Rule 3.

@pytest.mark.parametrize("fact", REQUIRED_FACTS)
def test_any_missing_required_fact_needs_clarification(fact):
    decision = route(record(**{fact: None}))
    assert placement(decision) == (SERVICE_DESK, "needs-information", "clarify")
    assert fact in " ".join(decision.reasons)


@pytest.mark.parametrize("changes, flags", [
    ({"kind": "other"}, None),
    ({"kind": None}, None),
    ({"issues": ["conflict: two environments"]}, None),
    ({"issues": ["id-mismatch: stated IT-100"]}, None),
    ({}, Flags(id_mismatch=True)),
    ({"start_date": "2026-11-06", "end_date": "2026-10-23"}, None),     # no issue, still caught
    ({}, Flags(date_order=True)),
    ({"business_purpose": "  "}, None),
])
def test_clarification_triggers(changes, flags):
    assert placement(route(record(**changes), flags)) == (SERVICE_DESK, "needs-information", "clarify")


def test_clarification_beats_risk():
    risky = record(environment="production", permission="admin", manager=None)
    assert route(risky).next_action == "clarify"


def test_issues_without_a_routing_code_do_not_block_a_complete_request():
    assert route(record(issues=["note: requester mentioned a past ticket"])).owner == SYSTEM_OWNER


# Rule 2.

def test_withdrawal_needs_only_id_and_date():
    withdrawal = record(kind="withdrawal", system=None, environment=None, permission=None, start_date=None,
                        end_date=None, business_purpose=None, manager=None)
    decision = route(withdrawal)
    assert placement(decision) == (SERVICE_DESK, "review", "confirm-withdrawal")
    assert "absent facts" in " ".join(decision.reasons)


@pytest.mark.parametrize("fact", ["request_id", "request_date"])
def test_withdrawal_without_id_or_date_falls_to_clarify(fact):
    assert route(record(kind="withdrawal", **{fact: None})).next_action == "clarify"


def test_withdrawal_beats_conflict():
    assert route(record(kind="withdrawal", issues=["conflict: two systems"])).next_action == "confirm-withdrawal"


# Rule 1.

@pytest.mark.parametrize("changes", [{}, {"kind": "withdrawal"}, {"kind": "other"}, {"manager": None},
                                     {"environment": "production", "permission": "admin"}])
def test_injection_beats_every_other_rule(changes):
    assert route(record(**changes), Flags(injection=True)).next_action == "inspect-message"
    assert route(record(issues=["injection: override attempt"], **changes)).next_action == "inspect-message"


def test_injection_routing_ignores_an_obeyed_permission(load, schema):
    """Even if a model obeyed IT-006's override (admin, no issue), the request goes to inspection."""
    from access_intake.llm import ModelResponse
    extraction = {**copy.deepcopy(FIXTURES["IT-006.json"]["extraction"]), "permission": "admin", "issues": []}
    document = load("IT-006.json")
    valid = validate_response(ModelResponse(json.dumps(extraction), "stop", "mock"), document, schema)
    assert placement(route(valid.extraction, valid.flags)) == (SERVICE_DESK, "review", "inspect-message")


# Errors, helpers and invariants.

def test_extraction_failure_placement():
    decision = route_error()
    assert placement(decision) == (SERVICE_DESK, "error", "retry-extraction")
    assert decision.as_dict()["policy_version"] == POLICY_VERSION


def test_missing_facts_by_kind():
    assert missing_facts(record()) == []
    assert missing_facts(record(kind="withdrawal", system=None)) == []
    assert missing_facts(record(kind="other", system=None)) == []
    assert missing_facts(record(end_date=None, manager=None)) == ["end_date", "manager"]


def test_no_rule_ever_approves_and_outcomes_are_known():
    cases = [record(), record(kind="withdrawal"), record(kind="other"), record(permission="admin"),
             record(issues=["approved: manager said yes"]), record(environment="production")]
    for case in cases:
        decision = route(case)
        assert placement(decision) in ALLOWED
        assert "approve" not in decision.status


def test_decision_as_dict():
    as_dict = route(record()).as_dict()
    assert as_dict == {"owner": SYSTEM_OWNER, "status": "review", "next_action": "review-request", "policy_rule": "5",
                       "reasons": ["complete staging read request"], "policy_version": POLICY_VERSION}
