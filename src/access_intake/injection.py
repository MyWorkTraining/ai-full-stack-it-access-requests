"""A deterministic backstop for instruction-override attempts inside a message.

The prompt already tells the model that the message is data and asks it to report such attempts.
This detector runs on the raw message in code, so routing to inspect-message does not depend on the
model noticing. It errs towards flagging: a false positive sends a request to IT Service Desk for a
look, which is safe; a miss could let a message steer the record.

Each pattern names one tactic. Matches are reported with their exact span, and evidence_for()
widens a match to its sentence, which is still an exact substring of the message, for use as a
supporting quote.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_FLAGS = re.IGNORECASE

PATTERNS: dict[str, re.Pattern[str]] = {
    "override-banner": re.compile(r"\b(?:system|admin|developer)\s+(?:override|prompt|message)\s*:", _FLAGS),
    "ignore-instructions": re.compile(
        r"\b(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)?"
        r"(?:previous|prior|above|earlier|preceding|system|existing)\s+(?:instructions?|rules|prompts?|guidelines)\b",
        _FLAGS),
    "bypass-controls": re.compile(
        r"\b(?:bypass|skip|circumvent|disable)\s+(?:the\s+|all\s+|any\s+)?(?:approval|review|security|policy|policies)\b",
        _FLAGS),
    "exfiltrate-secret": re.compile(
        r"\b(?:display|reveal|print|show|output|leak|send|include)\s+(?:me\s+)?(?:the\s+|your\s+|any\s+)?"
        r"(?:api[\s_-]?keys?|credentials?|secrets?|passwords?|access\s+tokens?|system\s+prompt)\b", _FLAGS),
    "priority-claim": re.compile(
        r"\b(?:highest|top|maximum)[\s-]priority\s+(?:system\s+)?(?:instructions?|commands?|directives?)\b", _FLAGS),
    "self-approval": re.compile(
        r"\b(?:mark|set|record|treat)\s+(?:this\s+|the\s+|my\s+)?(?:request\s+)?as\s+(?:already\s+)?approved\b", _FLAGS),
    "grant-immediately": re.compile(
        r"\bgrant\s+(?:me\s+)?(?:full\s+|admin\s+|administrator\s+|root\s+)access\s+(?:immediately|now|at\s+once)\b",
        _FLAGS),
    "role-change": re.compile(
        r"\b(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be)\s+(?:an?\s+|the\s+)?"
        r"(?:admin|administrator|system|approver|unrestricted)\b", _FLAGS),
    "delimiter-break": re.compile(r"</?\s*(?:message|system|instructions?|assistant)\s*>", _FLAGS),
}


@dataclass(frozen=True)
class InjectionFinding:
    """One matched tactic: its name and the exact matched text with its position in the message."""

    pattern: str
    text: str
    start: int
    end: int


def detect(message: str) -> list[InjectionFinding]:
    """Every instruction-override tactic found in the message, in order of position."""
    findings = [InjectionFinding(name, m.group(0), m.start(), m.end())
                for name, pattern in PATTERNS.items() for m in pattern.finditer(message)]
    return sorted(findings, key=lambda f: (f.start, f.pattern))


_SENTENCE_END = re.compile(r"[.!?](?=\s|$)|\n")


def evidence_for(message: str, finding: InjectionFinding) -> str:
    """The sentence holding a finding, as an exact substring of the message (trimmed)."""
    start = 0
    for boundary in _SENTENCE_END.finditer(message, 0, finding.start):
        start = boundary.end()
    after = _SENTENCE_END.search(message, finding.end)
    end = after.end() if after and message[after.start()] != "\n" else (after.start() if after else len(message))
    sentence = message[start:end]
    stripped = sentence.strip()
    return stripped if stripped else finding.text
