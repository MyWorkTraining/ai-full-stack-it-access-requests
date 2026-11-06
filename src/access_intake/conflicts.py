"""A deterministic backstop for contradictions inside one message.

The office policy says contradictory statements without a correction label stay unresolved (null,
with a conflict issue). The prompt asks the model for this, but a model can instead pick one value and
report nothing (observed live: IT-020's two managers). This module finds, in code, facts the message
states more than once with different values, for the fields where that can be read reliably from the
text:

  manager      "My manager is X", "X is my manager", "Manager is X", "The manager for this request is X",
               "My boss is X", "X is my boss", "X manages me"  (X = a First Last name)
  environment  the words "staging" and "production"
  permission   "read / write / admin access", "read-only access", "for reading / writing". Not checked
               when the message describes a change of access ("current", "change my", "transition",
               "from write to read", ...), where naming the old and the new permission is expected.
  start_date   the first date of a stated range ("from A to B", "A - B", "A through B", "during A and B")
               or a date after "start date", "starting", "beginning", "from"
  end_date     the last date of a range, or a date after "end date", "ending", "until", "through", "to"
               (a single stated date such as the request date is neither)

Text the caller excludes (the sentences of an instruction-override attempt, which are evidence, not
request facts) is ignored, so "Grant admin access immediately" does not contradict "read access".

A statement inside an explicit correction ("Correction: ...", or a parenthesised "(Correction: ...)")
resolves the field instead: if a correction mentions a value of a field, that field is not reported.
Each conflict carries the sentences that state the values, as exact substrings of the message.

It errs towards flagging: a false positive sends the request to IT Service Desk to clarify, which is
safe; a miss can route a contradictory request to an approver.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NAME = r"([A-Z][a-z]+ [A-Z][a-z]+)"
MANAGER_PATTERNS = [
    re.compile(r"\b(?:[Mm]y|[Tt]he)\s+(?:manager|boss)(?:\s+for\s+this\s+request)?\s+is\s+" + NAME),
    re.compile(r"\b[Mm]anager\s+is\s+" + NAME),
    re.compile(NAME + r"\s+is\s+my\s+(?:manager|boss)\b"),
    re.compile(NAME + r"\s+manages\s+me\b"),
]
ENVIRONMENT = re.compile(r"\b(staging|production)\b", re.IGNORECASE)
PERMISSION_PATTERNS = [
    re.compile(r"\b(read|write|admin)(?:[- ]only)?\s+access\b", re.IGNORECASE),
    re.compile(r"\bfor\s+(reading|writing)\b", re.IGNORECASE),
]
PERMISSION_WORDS = {"reading": "read", "writing": "write"}
CHANGE_CUES = re.compile(r"\b(?:current(?:ly)?|change\s+my|changing|transition(?:ing)?|switch(?:ing)?|upgrade|downgrade)\b"
                         r"|\bfrom\s+\(?(?:read|write|admin)\)?\s+to\s+\(?(?:read|write|admin)\)?", re.IGNORECASE)
DATE = r"(\d{4}-\d{2}-\d{2})"
DATE_RANGE = re.compile(DATE + r"\s*(?:-|\u2013|to|through|until|and)\s*" + DATE, re.IGNORECASE)
START_CUE = re.compile(r"\b(?:start(?:ing)?(?:\s+date)?|beginning|from)\s*:?\s*" + DATE, re.IGNORECASE)
END_CUE = re.compile(r"\b(?:end(?:ing)?(?:\s+date)?|until|through|to)\s*:?\s*" + DATE, re.IGNORECASE)
CORRECTION = re.compile(r"\bcorrection\b", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")
FIELDS = ("manager", "environment", "permission", "start_date", "end_date")


@dataclass(frozen=True)
class Conflict:
    """A field the message states with different values and no correction label."""

    field: str
    values: tuple[str, ...]          # distinct values, in order of first appearance
    evidence: tuple[str, ...]        # the sentences stating them, exact substrings of the message


def sentence_span(message: str, start: int, end: int) -> tuple[int, int]:
    """The (begin, finish) span of the sentence containing message[start:end]."""
    begin = 0
    for boundary in _SENTENCE_END.finditer(message, 0, start):
        begin = boundary.end()
    after = _SENTENCE_END.search(message, end)
    finish = after.end() if after and message[after.start()] != "\n" else (after.start() if after else len(message))
    return begin, finish


def sentence_at(message: str, start: int, end: int) -> str:
    """The sentence containing message[start:end], trimmed; an exact substring of the message."""
    begin, finish = sentence_span(message, start, end)
    return message[begin:finish].strip() or message[start:end]


def correction_spans(message: str) -> list[tuple[int, int]]:
    """Character spans of explicit corrections: "(Correction: ...)" or "Correction: ..." to sentence end."""
    spans = []
    for match in CORRECTION.finditer(message):
        start = match.start()
        opening = message.rfind("(", 0, start)
        if opening != -1 and message[opening:start].strip() in ("(", "") and ")" in message[start:]:
            spans.append((opening, message.index(")", start) + 1))
        else:
            after = _SENTENCE_END.search(message, match.end())
            spans.append((start, after.end() if after else len(message)))
    return spans


def _mentions(message: str, field: str) -> list[tuple[str, int, int]]:
    """(value, start, end) for each statement of the field's value."""
    found: list[tuple[str, int, int]] = []
    if field == "manager":
        for pattern in MANAGER_PATTERNS:
            found += [(m.group(1), m.start(), m.end()) for m in pattern.finditer(message)]
    elif field == "environment":
        found = [(m.group(1).lower(), m.start(), m.end()) for m in ENVIRONMENT.finditer(message)]
    elif field == "permission":
        if CHANGE_CUES.search(message):
            return []                     # a change request names its current and its new permission
        for pattern in PERMISSION_PATTERNS:
            for m in pattern.finditer(message):
                value = m.group(1).lower()
                found.append((PERMISSION_WORDS.get(value, value), m.start(), m.end()))
    elif field in ("start_date", "end_date"):
        group = 1 if field == "start_date" else 2
        found = [(m.group(group), m.start(group), m.end(group)) for m in DATE_RANGE.finditer(message)]
        cue = START_CUE if field == "start_date" else END_CUE
        found += [(m.group(1), m.start(1), m.end(1)) for m in cue.finditer(message)]
    return sorted(set(found), key=lambda item: item[1])


def detect_conflicts(message: str, exclude: list[tuple[int, int]] | tuple = ()) -> list[Conflict]:
    """Every field among FIELDS stated with two or more different values outside any correction.

    Args:
        message: the message text.
        exclude: character spans to ignore (e.g. the sentences of an instruction-override attempt).
    """
    spans = correction_spans(message)

    def corrected(start: int) -> bool:
        return any(a <= start < b for a, b in spans)

    def excluded(start: int) -> bool:
        return any(a <= start < b for a, b in exclude)

    conflicts = []
    for field in FIELDS:
        mentions = [m for m in _mentions(message, field) if not excluded(m[1])]
        if any(corrected(start) for _, start, _ in mentions):
            continue                          # an explicit correction settles this field
        values = tuple(dict.fromkeys(value for value, _, _ in mentions))
        if len(values) > 1:
            evidence = tuple(dict.fromkeys(sentence_at(message, s, e) for _, s, e in mentions))
            conflicts.append(Conflict(field, values, evidence))
    return conflicts
