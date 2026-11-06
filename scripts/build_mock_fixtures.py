"""Check (or re-stamp) the mock model's fixtures against their source messages.

    python scripts/build_mock_fixtures.py            check; exit 1 and list problems if any
    python scripts/build_mock_fixtures.py --stamp    after an intended source edit: write each
                                                     fixture's current source_sha256, then check

For every fixture in tests/fixtures/mock_responses.json it checks that:
  - its source file exists and loads as an export, and source_sha256 matches the file's bytes;
  - the extraction conforms to snapshot.schema.json;
  - every supporting quote is an exact substring of the message;
  - every non-null date is a real YYYY-MM-DD date;
  - every issue is "<code>: <explanation>" with a known code;
  - it agrees with tests/expected/expected_fields.json (the independently written truth).
It also checks coverage: every export in materials/requests/ has a fixture and an expected entry.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import jsonschema  # noqa: E402

from access_intake.issues import ISSUE_CODES, issue_code, issue_codes  # noqa: E402
from access_intake.loader import LoadError, load_document  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "mock_responses.json"
EXPECTED = ROOT / "tests" / "expected" / "expected_fields.json"
SCHEMA = ROOT / "snapshot.schema.json"
REQUESTS = ROOT / "materials" / "requests"
DATE_FIELDS = ("request_date", "start_date", "end_date")


def _is_date(value: str) -> bool:
    if len(value) != 10:
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_fixture(document_id: str, entry: dict[str, Any], schema: dict[str, Any], root: Path = ROOT) -> list[str]:
    """Problems with one fixture against its source message and the schema. Empty: none."""
    problems: list[str] = []
    source = root / entry.get("source", "")
    if not entry.get("source") or not source.is_file():
        return [f"source file not found: {entry.get('source')!r}"]
    if source.name != document_id:
        problems.append(f"source filename {source.name} differs from the fixture key {document_id}")
    try:
        document = load_document(source)
    except LoadError as exc:
        return [f"source does not load: {exc.detail}"]
    if entry.get("source_sha256") != document.source_sha256:
        problems.append(f"source_sha256 is {str(entry.get('source_sha256'))[:12]}, the file's is "
                        f"{document.source_sha256[:12]} (run with --stamp after an intended edit)")
    extraction = entry.get("extraction")
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(extraction), key=lambda e: list(e.path))
    problems += [f"schema: {'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in errors]
    if errors:
        return problems
    for quote in extraction["supporting_quotes"]:
        if quote not in document.message:
            problems.append(f"quote is not an exact substring of the message: {quote!r}")
    for name in DATE_FIELDS:
        value = extraction[name]
        if value is not None and not _is_date(value):
            problems.append(f"{name} is not a YYYY-MM-DD date: {value!r}")
    for issue in extraction["issues"]:
        if issue_code(issue) is None:
            problems.append(f"issue has no known code ({', '.join(ISSUE_CODES)}): {issue!r}")
    return problems


def compare_to_expected(extraction: dict[str, Any], expected: dict[str, Any], scalar_fields: list[str]) -> list[str]:
    """Differences between an extraction and the expected truth for its document. Empty: it agrees.

    Scalars are compared exactly after trimming whitespace (null must be null); business_purpose is
    null-vs-value plus case-insensitive keywords; required quotes must each lie within some
    supporting quote; the set of issue codes must be exactly the expected set.
    """
    problems: list[str] = []
    for name in scalar_fields:
        got, want = extraction.get(name), expected["fields"][name]
        got = got.strip() if isinstance(got, str) else got
        if got != want:
            problems.append(f"{name}: got {got!r}, expected {want!r}")
    purpose, rule = extraction.get("business_purpose"), expected["business_purpose"]
    if rule is None:
        if purpose is not None:
            problems.append(f"business_purpose: got {purpose!r}, expected null")
    elif not purpose:
        problems.append("business_purpose: got null, expected a stated purpose")
    else:
        absent = [k for k in rule["keywords"] if k.lower() not in purpose.lower()]
        if absent:
            problems.append(f"business_purpose lacks keywords {absent}: {purpose!r}")
    quotes = extraction.get("supporting_quotes") or []
    for required in expected["required_quotes"]:
        if not any(required in quote for quote in quotes):
            problems.append(f"supporting_quotes lack {required!r}")
    got_codes, want_codes = issue_codes(extraction.get("issues") or []), set(expected["issue_codes"])
    if got_codes != want_codes:
        problems.append(f"issue codes: got {sorted(got_codes)}, expected {sorted(want_codes)}")
    return problems


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_all(fixtures_path: Path = FIXTURES, expected_path: Path = EXPECTED, schema_path: Path = SCHEMA,
              requests_dir: Path = REQUESTS, root: Path = ROOT) -> dict[str, list[str]]:
    """{document_id or "(coverage)": problems} for every fixture; only entries with problems."""
    schema = load_json(schema_path)
    fixtures = load_json(fixtures_path)["documents"]
    expected_file = load_json(expected_path)
    expected, scalar_fields = expected_file["documents"], expected_file["scalar_fields"]
    report: dict[str, list[str]] = {}
    supplied = {p.name for p in requests_dir.glob("*.json")}
    coverage = [f"{name} has no mock fixture" for name in sorted(supplied - set(fixtures))]
    coverage += [f"{name} has no expected entry" for name in sorted((supplied | set(fixtures)) - set(expected))]
    if coverage:
        report["(coverage)"] = coverage
    for document_id, entry in sorted(fixtures.items()):
        problems = check_fixture(document_id, entry, schema, root)
        if document_id in expected and not any(p.startswith(("schema:", "source file not found", "source does not load")) for p in problems):
            problems += [f"vs expected: {p}" for p in
                         compare_to_expected(entry["extraction"], expected[document_id], scalar_fields)]
        if problems:
            report[document_id] = problems
    return report


def stamp(fixtures_path: Path = FIXTURES, root: Path = ROOT) -> list[str]:
    """Write each fixture's current source_sha256. Returns the documents whose hash changed."""
    data = load_json(fixtures_path)
    changed = []
    for document_id, entry in sorted(data["documents"].items()):
        digest = load_document(root / entry["source"]).source_sha256
        if entry.get("source_sha256") != digest:
            entry["source_sha256"] = digest
            changed.append(document_id)
    if changed:
        fixtures_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stamp", action="store_true", help="write current source hashes, then check")
    args = parser.parse_args(argv)
    if args.stamp:
        changed = stamp()
        print(f"Stamped {len(changed)} fixture(s): {', '.join(changed) or 'none changed'}")
    report = check_all()
    count = len(load_json(FIXTURES)["documents"])
    if not report:
        print(f"OK: {count} mock fixtures match their sources, the schema and the expected fields.")
        return 0
    for document_id, problems in report.items():
        print(f"{document_id}:")
        for problem in problems:
            print(f"  - {problem}")
    print(f"FAILED: problems in {len(report)} of {count} fixture(s).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
