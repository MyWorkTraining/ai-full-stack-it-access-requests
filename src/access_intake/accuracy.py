"""Field-level accuracy against the source-derived truth in tests/expected/expected_fields.json.

`intake accuracy` processes every document named in the expected file (the 20 supplied exports and
the paraphrase) with the configured model client into a separate state folder (<state_dir>/accuracy,
so the review queue is untouched), compares each record with the expected truth, and writes a
Markdown report labelled with the response source (mock, or provider/model) and configuration.

Counting rules:
  - 11 scalar fields per message are compared exactly (after trimming whitespace; names are
    case-sensitive; null must be null). Denominator: 11 x number of messages (231 for 21).
  - Each scalar comparison is one of: match; omission (truth has a value, the record has null);
    factual error (the record has a value and it differs from the truth, including a value where
    the truth is null). Factual errors are listed individually, and marked "not in message" when the
    value does not occur in the message text at all.
  - A document whose extraction failed (status error) scores none of its 11 scalars as a match:
    they are counted as "failed" (inside the denominator) and the document is listed separately.
  - business_purpose is a paraphrase, so it is scored separately: null where the truth is null, else
    non-null and containing every expected keyword (case-insensitive).
  - Also reported, not part of the 231: required supporting quotes present, issue-code set exactly
    equal to the expected set, and owner/status/next_action equal to the expected route.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .issues import issue_codes
from .loader import load_document

SCALAR_FIELDS = ("request_id", "request_date", "kind", "requester", "team", "system", "environment", "permission",
                 "start_date", "end_date", "manager")
FREE_TEXT = {"request_id", "requester", "team", "system", "manager"}   # checked for presence in the message


@dataclass
class Mismatch:
    document_id: str
    check: str            # a field name, or business_purpose / quotes / issues / route / extraction
    outcome: str          # omission | factual-error | mismatch | failed
    expected: Any
    got: Any
    note: str = ""


@dataclass
class AccuracyReport:
    documents: list[str]
    response_sources: list[str]
    field_counts: dict[str, Counter] = field(default_factory=dict)      # field -> Counter(outcome)
    purpose: Counter = field(default_factory=Counter)
    quotes: Counter = field(default_factory=Counter)
    issues: Counter = field(default_factory=Counter)
    routes: Counter = field(default_factory=Counter)
    failed: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def scalar_total(self) -> int:
        return len(SCALAR_FIELDS) * len(self.documents)

    def scalar(self, outcome: str) -> int:
        return sum(c[outcome] for c in self.field_counts.values())

    def factual_errors(self) -> list[Mismatch]:
        return [m for m in self.mismatches if m.outcome == "factual-error"]


def _clean(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def compare(records: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]], root: Path) -> AccuracyReport:
    """Compare current records with the expected truth for every expected document."""
    report = AccuracyReport(documents=sorted(expected),
                            response_sources=sorted({str(r.get("response_source")) for r in records.values()}))
    report.field_counts = {name: Counter() for name in SCALAR_FIELDS}
    for document_id in report.documents:
        truth, record = expected[document_id], records.get(document_id)
        message = load_document(root / truth["source"]).message
        extraction = (record or {}).get("extraction")
        if extraction is None:
            report.failed.append(document_id)
            code = ((record or {}).get("error") or {}).get("code", "no record")
            report.mismatches.append(Mismatch(document_id, "extraction", "failed", "a valid extraction", None, code))
            for name in SCALAR_FIELDS:
                report.field_counts[name]["failed"] += 1
            report.purpose["fail"] += 1
            report.quotes["fail"] += 1
            report.issues["fail"] += 1
        else:
            for name in SCALAR_FIELDS:
                want, got = truth["fields"][name], _clean(extraction.get(name))
                if got == want:
                    outcome = "match"
                elif got is None:
                    outcome = "omission"
                else:
                    outcome = "factual-error"
                report.field_counts[name][outcome] += 1
                if outcome != "match":
                    note = ""
                    if outcome == "factual-error" and name in FREE_TEXT and str(got).lower() not in message.lower():
                        note = "not in message"
                    report.mismatches.append(Mismatch(document_id, name, outcome, want, got, note))
            purpose, rule = extraction.get("business_purpose"), truth["business_purpose"]
            if rule is None:
                ok = purpose is None
            else:
                ok = bool(purpose) and all(k.lower() in purpose.lower() for k in rule["keywords"])
            report.purpose["pass" if ok else "fail"] += 1
            if not ok:
                report.mismatches.append(Mismatch(document_id, "business_purpose", "mismatch",
                                                  None if rule is None else {"keywords": rule["keywords"]}, purpose))
            quotes = extraction.get("supporting_quotes") or []
            absent = [q for q in truth["required_quotes"] if not any(q in s for s in quotes)]
            report.quotes["pass" if not absent else "fail"] += 1
            if absent:
                report.mismatches.append(Mismatch(document_id, "quotes", "mismatch", absent, quotes))
            got_codes, want_codes = issue_codes(extraction.get("issues") or []), set(truth["issue_codes"])
            report.issues["pass" if got_codes == want_codes else "fail"] += 1
            if got_codes != want_codes:
                report.mismatches.append(Mismatch(document_id, "issues", "mismatch", sorted(want_codes),
                                                  sorted(got_codes)))
        route = truth["route"]
        want_route = (route["owner"], route["status"], route["next_action"])
        got_route = tuple((record or {}).get(k) for k in ("owner", "status", "next_action"))
        report.routes["pass" if got_route == want_route else "fail"] += 1
        if got_route != want_route:
            report.mismatches.append(Mismatch(document_id, "route", "mismatch", " / ".join(want_route),
                                              " / ".join(str(x) for x in got_route)))
    return report


def _cell(value: Any) -> str:
    return "null" if value is None else str(value).replace("|", "\\|")


def render_markdown(report: AccuracyReport, expected: dict[str, dict[str, Any]], meta: dict[str, Any]) -> str:
    """The accuracy report as Markdown."""
    sources = ", ".join(report.response_sources)
    mocked = report.response_sources == ["mock"]
    n = len(report.documents)
    lines = [
        "# Extraction accuracy",
        "",
        f"Generated by `intake accuracy` on {meta['generated_at']}.",
        "",
        "## Configuration of this observation",
        "",
        f"- Response source: **{sources}** "
        + ("(MOCKED responses: canned fixtures from tests/fixtures/mock_responses.json; this demonstrates the "
           "comparison procedure and the pipeline, not model accuracy)" if mocked else "(live provider responses)"),
        f"- Provider setting: `{meta['provider']}`, model ID: `{meta['model_id']}`, parameters: `{meta['params']}`",
        f"- Prompt version: `{meta['prompt_version']}`; policy version: `{meta['policy_version']}`",
        f"- Settings fingerprint: `{meta['settings_fingerprint']}`",
        f"- Expected truth: `{meta['expected_path']}` ({n} messages: "
        f"{sum(1 for d in expected.values() if not d.get('synthetic'))} supplied, "
        f"{sum(1 for d in expected.values() if d.get('synthetic'))} authored paraphrase)",
        "- Live provider observations: none recorded in this report" if mocked else
        "- Live provider observation: this report",
        "",
        "## Results",
        "",
        "| Measure | Result |",
        "|---|---|",
        f"| Scalar fields, exact match | {report.scalar('match')} / {report.scalar_total} |",
        f"| Omissions (truth has a value, record null) | {report.scalar('omission')} |",
        f"| Factual errors (record value differs from truth) | {report.scalar('factual-error')} |",
        f"| Not compared: failed extraction (counted as non-matches) | {report.scalar('failed')} |",
        f"| Failed extractions (error records) | {len(report.failed)} / {n} |",
        f"| business_purpose (null rule or all keywords) | {report.purpose['pass']} / {n} |",
        f"| Required supporting quotes present | {report.quotes['pass']} / {n} |",
        f"| Issue-code set equals expected | {report.issues['pass']} / {n} |",
        f"| Owner / status / next_action equal expected | {report.routes['pass']} / {n} |",
        "",
        "| Field | Match | Omission | Factual error |",
        "|---|---|---|---|",
    ]
    for name in SCALAR_FIELDS:
        c = report.field_counts[name]
        lines.append(f"| {name} | {c['match']} / {n} | {c['omission']} | {c['factual-error']} |")
    lines += ["", "### Factual errors", ""]
    errors = report.factual_errors()
    lines += ([f"- {m.document_id} `{m.check}`: expected {_cell(m.expected)}, got {_cell(m.got)}"
               + (f" ({m.note})" if m.note else "") for m in errors] or ["None."])
    lines += ["", "### Other differences", ""]
    others = [m for m in report.mismatches if m.outcome != "factual-error"]
    lines += ([f"- {m.document_id} `{m.check}` ({m.outcome}): expected {_cell(m.expected)}, got {_cell(m.got)}"
               + (f" [{m.note}]" if m.note else "") for m in others] or ["None."])
    lines += [
        "",
        "## Comparison procedure",
        "",
        "1. The expected facts in `tests/expected/expected_fields.json` were written from each message and "
        "`materials/office-policy.md`, independently of any model output.",
        "2. `intake accuracy` processes every expected document with the configured client into "
        "`<state_dir>/accuracy/` (the review queue is not touched), validates each reply exactly as "
        "`intake run` does, then compares the current records with the truth.",
        "3. Scalars (11 per message: " + ", ".join(SCALAR_FIELDS) + ") are compared exactly after trimming "
        f"whitespace; null must equal null. Denominator: 11 x {n} = {report.scalar_total}.",
        "4. Each non-match is an omission (record null, truth non-null) or a factual error (record non-null "
        "and different, including non-null where the truth is null). Factual errors are listed one by one; "
        "free-text values that do not occur in the message are marked \"not in message\".",
        "5. A failed extraction scores none of its 11 scalars as a match (they stay in the denominator as "
        "failed) and is listed under other differences with its error code.",
        "6. business_purpose, required quotes, issue codes and the route are scored separately (pass/fail per "
        "message) and are not part of the scalar denominator.",
        "",
        "## Expected fields (source-derived)",
        "",
        "`null` means the policy requires null (with an issue). Quotes and issue codes required for each message "
        "are in the expected file.",
        "",
        "| Doc | " + " | ".join(SCALAR_FIELDS) + " | issue codes | route |",
        "|" + "---|" * (len(SCALAR_FIELDS) + 3),
    ]
    for document_id in report.documents:
        truth = expected[document_id]
        label = document_id + (" (paraphrase)" if truth.get("synthetic") else "")
        route = truth["route"]
        lines.append(f"| {label} | " + " | ".join(_cell(truth["fields"][f]) for f in SCALAR_FIELDS)
                     + f" | {', '.join(truth['issue_codes']) or '-'} | {route['owner']} / {route['status']} / "
                       f"{route['next_action']} |")
    lines += ["", "business_purpose keywords:", ""]
    for document_id in report.documents:
        rule = expected[document_id]["business_purpose"]
        lines.append(f"- {document_id}: " + ("null" if rule is None else ", ".join(rule["keywords"])))
    lines += [
        "",
        "## Limits",
        "",
        "- With the mock, the replies are fixtures written to match the truth, so a perfect score shows that "
        "the pipeline, validation, routing and comparison work end to end; it is not evidence of model "
        "accuracy. Run the same command with a configured provider to measure a model, and keep each report "
        "labelled with its response source and configuration.",
        "- One paraphrase exercises fresh wording; 21 messages are a small sample, so live results should be "
        "read as a smoke check, not a statistically meaningful accuracy estimate.",
        "",
    ]
    return "\n".join(lines)


def load_expected(path: str | Path) -> dict[str, dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["documents"]


def run_accuracy(settings, expected_path: str | Path, out_path: str | Path | None = None, *, client=None,
                 root: Path | None = None, sleep=None) -> tuple[AccuracyReport, str]:
    """Process the expected documents into <state_dir>/accuracy, compare, and optionally write the report."""
    from .pipeline import Pipeline
    from .store import atomic_write
    root = Path.cwd() if root is None else Path(root)
    expected = load_expected(expected_path)
    base = Path(settings.state_dir) / "accuracy"
    scratch = dataclasses.replace(settings, state_dir=base / "state", output_dir=base / "out")
    kwargs = {} if sleep is None else {"sleep": sleep}
    pipeline = Pipeline(scratch, client, **kwargs)
    pipeline.run([root / truth["source"] for truth in expected.values()])
    records = {r["document_id"]: r for r in pipeline.store.all_current() if r["document_id"] in expected}
    report = compare(records, expected, root)
    full_fp, _, policy_version = pipeline.fingerprints()
    from .prompt import PROMPT_VERSION
    meta = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "provider": settings.provider, "model_id": settings.model_id, "params": settings.model_params(),
            "prompt_version": PROMPT_VERSION, "policy_version": policy_version, "settings_fingerprint": full_fp,
            "expected_path": Path(expected_path).as_posix()}
    text = render_markdown(report, expected, meta)
    if out_path is not None:
        atomic_write(Path(out_path), text)
    return report, text
