"""The review-queue policy (materials/office-policy.md, "Review queue"), applied in code.

Rules, in order; the first that applies decides:
  1. instruction-override attempt          -> IT Service Desk / review / inspect-message
  2. withdrawal with request_id and date   -> IT Service Desk / review / confirm-withdrawal
  3. kind other or null, a conflict, an identifier mismatch, a missing required fact, or
     start_date after end_date             -> IT Service Desk / needs-information / clarify
  4. complete new/change involving production, write or admin
                                           -> Security Review / review / review-risk-then-owner
  5. complete staging + read new/change    -> System Owner / review / review-request
An extraction failure (no valid extraction) -> IT Service Desk / error / retry-extraction.

Routing reads only structured values: the enum fields, which facts are null, issue codes and the
validator's flags. Free text in the message or in an issue explanation cannot change the outcome,
and nothing here approves a request: every successful outcome awaits a named human reviewer.
Change POLICY_VERSION whenever these rules change, so stored records are re-routed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .issues import Flags, issue_codes

# .2: conflicts detected in code (access_intake.conflicts) count for rule 3 even when the model reports none.
# .3: the code-level conflict check also covers permission, start_date and end_date.
# .4: the validator merges near-duplicate supporting quotes. Changes to the deterministic record checks
#     are versioned here too, so stored records are re-checked (re-route, no model call) on the next run.
POLICY_VERSION = "northstar-intake-policy/2026-10-22.4"

SERVICE_DESK = "IT Service Desk"
SECURITY_REVIEW = "Security Review"
SYSTEM_OWNER = "System Owner"

# Required facts for new and change requests (policy rule 3).
REQUIRED_FACTS = ("request_id", "request_date", "requester", "team", "system", "environment", "permission",
                  "business_purpose", "start_date", "end_date", "manager")
# A withdrawal needs only these to be confirmed (policy rule 2).
WITHDRAWAL_FACTS = ("request_id", "request_date")


@dataclass(frozen=True)
class QueueDecision:
    """Where a record goes in the review queue, and why."""

    owner: str
    status: str
    next_action: str
    rule: str              # "1".."5", or "extraction-failure"
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"owner": self.owner, "status": self.status, "next_action": self.next_action,
                "policy_rule": self.rule, "reasons": list(self.reasons), "policy_version": POLICY_VERSION}


def required_facts(kind: str | None) -> tuple[str, ...]:
    """The facts that must be non-null for a request of this kind to be complete."""
    if kind in ("new", "change"):
        return REQUIRED_FACTS
    if kind == "withdrawal":
        return WITHDRAWAL_FACTS
    return ()


def _absent(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def missing_facts(extraction: dict[str, Any]) -> list[str]:
    """The required facts of this record's kind that are null or blank."""
    return [name for name in required_facts(extraction.get("kind")) if _absent(extraction.get(name))]


def route_error() -> QueueDecision:
    """The placement for a document whose extraction failed."""
    return QueueDecision(SERVICE_DESK, "error", "retry-extraction", "extraction-failure",
                         ("no valid extraction; see the record's error for the retry command",))


def route(extraction: dict[str, Any], flags: Flags | None = None) -> QueueDecision:
    """Apply the five policy rules, in order, to one validated extraction."""
    flags = flags or Flags()
    codes = issue_codes(extraction.get("issues") or [])
    kind = extraction.get("kind")

    # Rule 1. Checked first so that nothing a message says can move it past inspection.
    if flags.injection or "injection" in codes:
        return QueueDecision(SERVICE_DESK, "review", "inspect-message", "1",
                             ("the message contains an instruction-override attempt",))

    # Rule 2.
    if kind == "withdrawal" and not missing_facts(extraction):
        absent = [n for n in REQUIRED_FACTS if _absent(extraction.get(n))]
        reasons = ["withdrawal with request_id and request_date"]
        if absent:
            reasons.append("absent facts: " + ", ".join(absent))
        return QueueDecision(SERVICE_DESK, "review", "confirm-withdrawal", "2", tuple(reasons))

    # Rule 3.
    reasons: list[str] = []
    if kind not in ("new", "change", "withdrawal"):
        reasons.append(f"kind is {kind or 'null'}")
    if "conflict" in codes or flags.conflicts:
        fields = f" ({', '.join(flags.conflicts)})" if flags.conflicts else ""
        reasons.append(f"conflicting details{fields}")
    if flags.id_mismatch or "id-mismatch" in codes:
        reasons.append("identifier mismatch")
    absent = missing_facts(extraction)
    if absent:
        reasons.append("missing required facts: " + ", ".join(absent))
    start, end = extraction.get("start_date"), extraction.get("end_date")
    if flags.date_order or "date-order" in codes or (start and end and start > end):
        reasons.append("start_date is after end_date")
    if reasons:
        return QueueDecision(SERVICE_DESK, "needs-information", "clarify", "3", tuple(reasons))

    # Rules 4 and 5: a complete new or change request.
    environment, permission = extraction.get("environment"), extraction.get("permission")
    risky = [r for r, hit in (("production", environment == "production"), (permission, permission in ("write", "admin")))
             if hit]
    if risky:
        return QueueDecision(SECURITY_REVIEW, "review", "review-risk-then-owner", "4",
                             ("involves " + " and ".join(risky),))
    if environment == "staging" and permission == "read":
        return QueueDecision(SYSTEM_OWNER, "review", "review-request", "5", ("complete staging read request",))

    # Unreachable for schema-valid records; kept so an unexpected value is never auto-routed onward.
    return QueueDecision(SERVICE_DESK, "needs-information", "clarify", "3",
                         (f"unrecognised environment/permission: {environment}/{permission}",))
