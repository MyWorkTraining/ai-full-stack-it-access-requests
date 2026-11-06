"""Validate a model reply before any of it is used, then add the facts code can establish itself.

Order (the first failure wins, and nothing from a failed reply is used):
  1. completion status  refusal -> refused, content_filter -> content-filtered, length -> truncated,
                        no text -> empty-output
  2. JSON parse         one JSON object (optionally inside a single ```json fence) -> else malformed-output
  3. schema             snapshot.schema.json, additionalProperties false, enums -> else schema-violation
  4. grounding          every supporting quote is an exact substring of the message -> else ungrounded-quote
  5. dates              non-null dates are real YYYY-MM-DD dates -> else invalid-date

Then, deterministically and idempotently (an issue is added only if its code is absent):
  - the stated request_id differs from the filename's identifier -> id-mismatch issue
  - start_date is after end_date -> date-order issue
  - the injection detector matches -> injection issue, and the matched sentence as a quote
  - the conflict detector finds a field stated with different values and no correction (manager,
    environment, permission, start_date, end_date; instruction-override sentences are ignored) ->
    that field is set to null, a conflict issue names it, and both statements are added as quotes
    (the policy keeps contradictions unresolved even if the model picked one)
  - a required fact is null and no issue explains any absence -> missing issue
  - near-duplicate supporting quotes are merged: a quote contained in another quote ("My manager is
    Priya Shah" inside "My manager is Priya Shah.") is dropped, the longer one keeps the earlier position
Issue codes are normalised ("Conflict : x" -> "conflict: x"). The model can only add information
here; it cannot remove a flag that code raised, and routing is computed later from both.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any

import jsonschema

from .conflicts import detect_conflicts, sentence_span
from .injection import detect, evidence_for
from .issues import Flags, issue_codes, normalise_issue
from .llm.base import ModelResponse
from .loader import SourceDocument
from .policy import required_facts

DATE_FIELDS = ("request_date", "start_date", "end_date")
# Issue codes that already explain a null fact, so the missing backstop adds nothing.
EXPLAINS_ABSENCE = {"missing", "conflict", "relative-date", "not-access-request"}
_FENCED = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def retry_command(document_id: str) -> str:
    """The command an operator runs to reprocess one document."""
    return f"intake retry {document_id}"


class ExtractionError(Exception):
    """A reply that cannot be used. The record gets status error and this actionable message."""

    def __init__(self, code: str, detail: str, document_id: str, retryable: bool = False) -> None:
        self.code = code
        self.detail = detail
        self.document_id = document_id
        self.retryable = retryable
        self.retry_command = retry_command(document_id)
        super().__init__(f"{code}: {detail}. Retry with: {self.retry_command}")

    def as_record(self) -> dict[str, Any]:
        """The error object written into an extractions.jsonl record."""
        return {"code": self.code, "message": str(self), "retry_command": self.retry_command}


@dataclass(frozen=True)
class ValidExtraction:
    """A reply that passed every check, with the issues and flags code added."""

    extraction: dict[str, Any]
    flags: Flags
    added_issues: tuple[str, ...]
    added_quotes: tuple[str, ...]
    merged_quotes: tuple[str, ...] = ()     # quotes dropped because a longer quote contains them


def _parse(text: str, document_id: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = _FENCED.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ExtractionError("malformed-output", f"model reply is not valid JSON (line {exc.lineno} "
                              f"column {exc.colno})", document_id) from None
    if not isinstance(value, dict):
        raise ExtractionError("malformed-output", f"model reply is JSON {type(value).__name__}, not an object",
                              document_id)
    return value


def _check_completion(response: ModelResponse, document_id: str) -> str:
    if response.finish_reason == "refusal":
        raise ExtractionError("refused", "the model refused to extract this message"
                              + (f" ({response.refusal[:200]})" if response.refusal else ""), document_id)
    if response.finish_reason == "content_filter":
        raise ExtractionError("content-filtered", "the provider's content filter stopped the reply", document_id)
    if response.finish_reason == "length":
        raise ExtractionError("truncated", "the reply was cut off before it finished (output token limit or "
                              "interrupted response)", document_id)
    if response.text is None or not response.text.strip():
        raise ExtractionError("empty-output", "the model returned no text", document_id)
    return response.text


def _is_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def merge_quotes(quotes: list[str]) -> list[str]:
    """Drop each quote contained in another quote; order by the first position of each merged group.

    Every kept quote is one of the inputs, so it is still an exact substring of the message.
    """
    unique = list(dict.fromkeys(quotes))
    keep = [q for q in unique if not any(q != other and q in other for other in unique)]
    first = {q: min(i for i, other in enumerate(unique) if other in q) for q in keep}
    return sorted(keep, key=first.__getitem__)


def _same_id(stated: str, stem: str) -> bool:
    return re.sub(r"\s+", "", stated).upper() == re.sub(r"\s+", "", stem).upper()


def validate_extraction(value: Any, document: SourceDocument, schema: dict[str, Any]) -> ValidExtraction:
    """Check a parsed reply against the schema and the message, and add code-established facts.

    Raises:
        ExtractionError: schema-violation, ungrounded-quote or invalid-date.
    """
    document_id = document.document_id
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        shown = "; ".join(f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message[:120]}" for e in errors[:3])
        more = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
        raise ExtractionError("schema-violation", f"reply does not match snapshot.schema.json: {shown}{more}",
                              document_id)
    extraction = copy.deepcopy(value)
    for name, item in extraction.items():
        if isinstance(item, str):
            extraction[name] = item.strip() or None   # an empty string is an absent value

    quotes: list[str] = []
    for quote in extraction["supporting_quotes"]:
        if not quote.strip():
            continue
        if quote not in document.message:
            raise ExtractionError("ungrounded-quote", f"supporting quote is not an exact substring of the "
                                  f"message: {quote[:120]!r}", document_id)
        if quote not in quotes:
            quotes.append(quote)
    for name in DATE_FIELDS:
        if extraction[name] is not None and not _is_date(extraction[name]):
            raise ExtractionError("invalid-date", f"{name} is not a real YYYY-MM-DD date: "
                                  f"{extraction[name][:40]!r}", document_id)

    issues: list[str] = []
    for issue in map(normalise_issue, extraction["issues"]):
        if issue and issue not in issues:
            issues.append(issue)
    added_issues: list[str] = []
    added_quotes: list[str] = []

    def add_issue(code: str, text: str) -> None:
        if code not in issue_codes(issues):
            issues.append(f"{code}: {text}")
            added_issues.append(issues[-1])

    def add_quote(quote: str) -> None:
        if not any(quote in existing for existing in quotes):
            quotes.append(quote)
            added_quotes.append(quote)

    stated = extraction["request_id"]
    id_mismatch = stated is not None and not _same_id(stated, document.stem)
    if id_mismatch:
        add_issue("id-mismatch", f"the message states request {stated} but the document filename is "
                                 f"{document_id}; the stated identifier is kept")
        if stated in document.message:
            add_quote(stated)

    start, end = extraction["start_date"], extraction["end_date"]
    date_order = start is not None and end is not None and start > end
    if date_order:
        add_issue("date-order", f"start_date {start} is after end_date {end}")

    findings = detect(document.message)
    evidence: list[str] = []
    for finding in findings:
        sentence = evidence_for(document.message, finding)
        evidence.append(sentence if finding.text in sentence else finding.text)
    evidence = list(dict.fromkeys(evidence))
    if findings:
        add_issue("injection", f"the message contains an instruction-override attempt (\"{findings[0].text}\"); "
                               "treated as data and not followed")
        for sentence in evidence:
            add_quote(sentence)

    override_sentences = [sentence_span(document.message, f.start, f.end) for f in findings]
    conflicts = detect_conflicts(document.message, exclude=override_sentences)
    for conflict in conflicts:
        chosen = extraction[conflict.field]
        extraction[conflict.field] = None
        stated = " and ".join(repr(v) for v in conflict.values)
        already = any(issue.startswith("conflict:") and conflict.field in issue for issue in issues)
        if not already:
            note = f"; the model's value {chosen!r} was set to null" if chosen is not None else ""
            issues.append(f"conflict: {conflict.field} stated as {stated} with no correction label; left "
                          f"unresolved (detected in code){note}")
            added_issues.append(issues[-1])
        elif chosen is not None:
            issues.append(f"conflict: {conflict.field} value {chosen!r} was set to null; the message states "
                          f"{stated} with no correction label (detected in code)")
            added_issues.append(issues[-1])
        for sentence in conflict.evidence:
            add_quote(sentence)

    absent = [name for name in required_facts(extraction["kind"]) if extraction[name] is None]
    if absent and not (issue_codes(issues) & EXPLAINS_ABSENCE):
        add_issue("missing", f"{', '.join(absent)} not stated")

    merged = merge_quotes(quotes)
    dropped = tuple(q for q in quotes if q not in merged)
    added_quotes = [q for q in added_quotes if q in merged]
    extraction["supporting_quotes"] = merged
    extraction["issues"] = issues
    jsonschema.Draft202012Validator(schema).validate(extraction)   # additions keep the record valid
    flags = Flags(injection=bool(findings) or "injection" in issue_codes(issues),
                  injection_evidence=tuple(evidence), id_mismatch=id_mismatch, date_order=date_order,
                  conflicts=tuple(c.field for c in conflicts))
    return ValidExtraction(extraction, flags, tuple(added_issues), tuple(added_quotes), dropped)


def validate_response(response: ModelResponse, document: SourceDocument, schema: dict[str, Any]) -> ValidExtraction:
    """Validate one model reply for one document, in the order in the module docstring.

    Raises:
        ExtractionError: the reply cannot be used; its code says why and it carries the retry command.
    """
    text = _check_completion(response, document.document_id)
    return validate_extraction(_parse(text, document.document_id), document, schema)
