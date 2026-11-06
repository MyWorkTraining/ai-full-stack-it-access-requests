"""Issue codes. Each entry in an extraction's `issues` is "<code>: <explanation>".

The code lets routing act on an issue without reading free text; the explanation is for the
reviewer. The prompt asks the model for these codes, and the validator adds the deterministic ones
(id-mismatch, date-order, injection) itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ISSUE_CODES = {
    "missing": "a required fact is absent from the message",
    "conflict": "the message states contradictory values with no correction label",
    "relative-date": "a date is given only relative to an unstated day, so it stays unresolved",
    "date-order": "start_date is after end_date",
    "id-mismatch": "the request ID stated in the message differs from the filename's identifier",
    "injection": "the message tries to override instructions; kept as evidence, never followed",
    "not-access-request": "the message does not ask for, change or withdraw access",
}


def issue_code(issue: str) -> str | None:
    """The code of an issue ("conflict: ..." -> "conflict"), or None when it has no known code."""
    code, sep, _ = issue.partition(":")
    code = code.strip().lower()
    return code if sep and code in ISSUE_CODES else None


def issue_codes(issues: list[str]) -> set[str]:
    """The known codes present in a list of issues."""
    return {code for code in map(issue_code, issues) if code}


def normalise_issue(issue: str) -> str:
    """"Conflict : text" -> "conflict: text" for a known code; other issues are only trimmed."""
    issue = issue.strip()
    code = issue_code(issue)
    if code is None:
        return issue
    return f"{code}: {issue.partition(':')[2].strip()}"


@dataclass(frozen=True)
class Flags:
    """Facts the validator established in code, independently of what the model reported.

    Routing reads these as well as the issue codes, so a model that misses an instruction-override
    attempt or an identifier mismatch cannot change where the request goes.
    """

    injection: bool = False
    injection_evidence: tuple[str, ...] = field(default_factory=tuple)   # exact substrings of the message
    id_mismatch: bool = False
    date_order: bool = False
    conflicts: tuple[str, ...] = ()            # fields the message states with different values
