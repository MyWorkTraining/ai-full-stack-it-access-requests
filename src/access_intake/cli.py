"""The `intake` command line: start intake, inspect the queue while work is pending, follow up on failures.

    intake run materials/requests [--force]     process exports; unchanged documents are skipped
    intake status [--run RUN_ID]                the latest run's progress, queue counts, errors, stale records
    intake queue [--owner O] [--status S]       the current review queue (works while a run is in progress)
    intake show IT-004.json [--history]         one record: facts, quotes, issues, routing, provenance
    intake retry IT-002.json | --errors         reprocess failed documents (each error names its command)
    intake resume [RUN_ID]                      continue an interrupted or halted run's open documents
    intake accuracy [--out deliverables/accuracy.md]       field accuracy against the expected truth
    intake evidence [EXPORT] [--out deliverables/request-evidence.json]  example request + handling

Global options (before the command) override the settings file and INTAKE_* environment variables:
--config, --state-dir, --output-dir, --provider, --model-id, --concurrency, -v.

Exit status: 0 finished with no error records; 1 finished, but some documents are errors; 2 usage or
configuration problem; 3 halted or incomplete (resume later); 130 interrupted.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from . import policy
from .config import ConfigError, Settings, load_schema, load_settings, settings_fingerprint
from .logging_redact import get_logger
from .prompt import PROMPT_VERSION
from .store import OPEN_STATES, Store

EXIT_OK, EXIT_ERRORS, EXIT_USAGE, EXIT_UNFINISHED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intake", description="Northstar Labs IT access-request intake.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="Exit status: 0 ok, 1 error records, 2 usage, 3 halted/incomplete, "
                                            "130 interrupted.")
    parser.add_argument("--config", type=Path, help="a TOML settings file ([intake] table)")
    parser.add_argument("--state-dir", type=Path, help="state folder (default: state)")
    parser.add_argument("--output-dir", type=Path, help="where extractions.jsonl and queue.json go (default: deliverables)")
    parser.add_argument("--provider", choices=("mock", "databricks", "provider"),
                        help="model client: mock (default), databricks (a serving endpoint), provider (placeholder)")
    parser.add_argument("--model-id", help="model identifier; for databricks, the serving endpoint name")
    parser.add_argument("--concurrency", type=int, help="parallel model calls (default: 4)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log each step to stderr (credentials redacted)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", help="process export files or folders")
    run.add_argument("paths", nargs="+", type=Path, help="export files or folders of *.json exports")
    run.add_argument("--force", action="store_true", help="call the model even for unchanged documents")

    status = commands.add_parser("status", help="progress of the latest run, queue counts and errors")
    status.add_argument("--run", dest="run_id", help="a specific run id")

    queue = commands.add_parser("queue", help="list the current review queue")
    queue.add_argument("--owner", help='e.g. "Security Review"')
    queue.add_argument("--status", dest="status_filter", help="review | needs-information | error")
    queue.add_argument("--next-action", help="e.g. clarify")
    queue.add_argument("--json", action="store_true", help="print matching queue entries as JSON")

    show = commands.add_parser("show", help="one document's current record")
    show.add_argument("document_id", help="e.g. IT-004.json (or IT-004)")
    show.add_argument("--history", action="store_true", help="also list every stored attempt")
    show.add_argument("--json", action="store_true", help="print the record as JSON")

    retry = commands.add_parser("retry", help="reprocess failed or named documents")
    retry.add_argument("document_ids", nargs="*", help="e.g. IT-002.json")
    retry.add_argument("--errors", action="store_true", help="every document whose current record is an error")
    retry.add_argument("--force", action="store_true", help="call the model even if a stored result could be reused")

    resume = commands.add_parser("resume", help="continue a run's pending, running or deferred documents")
    resume.add_argument("run_id", nargs="?", help="default: the latest run with open documents")

    accuracy = commands.add_parser("accuracy", help="compare extractions with the source-derived expected fields")
    accuracy.add_argument("--expected", type=Path, default=Path("tests/expected/expected_fields.json"))
    accuracy.add_argument("--out", type=Path, help="write the Markdown report here (e.g. deliverables/accuracy.md)")

    evidence = commands.add_parser("evidence", help="write a credential-redacted example request and its handling")
    evidence.add_argument("export", nargs="?", type=Path, default=Path("materials/requests/IT-006.json"))
    evidence.add_argument("--out", type=Path, default=Path("deliverables/request-evidence.json"))
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(args.config, state_dir=args.state_dir, output_dir=args.output_dir, provider=args.provider,
                         model_id=args.model_id, concurrency=args.concurrency)


def _store(settings: Settings) -> Store:
    return Store(settings.state_dir, settings.output_dir)


def _norm(document_id: str) -> str:
    return document_id if document_id.endswith(".json") else document_id + ".json"


def _current_fingerprint(settings: Settings) -> str | None:
    try:
        _, schema_sha256 = load_schema(settings.schema_path)
    except OSError:
        return None
    return settings_fingerprint(settings, prompt_version=PROMPT_VERSION, policy_version=policy.POLICY_VERSION,
                                schema_sha256=schema_sha256)


def _print_outcome(outcome) -> None:
    route = " / ".join(x for x in (outcome.owner, outcome.status, outcome.next_action) if x) or "-"
    line = f"{outcome.document_id:<16} {outcome.state:<9} {route}"
    if outcome.error_code:
        line += f"  [{outcome.error_code}] retry: intake retry {outcome.document_id}"
    elif outcome.state == "deferred" and outcome.message:
        line += f"  ({outcome.message})"
    print(line, flush=True)


def _finish(result, store: Store) -> int:
    states = result.states()
    summary = ", ".join(f"{n} {s}" for s, n in sorted(states.items())) or "nothing to do"
    print(f"\nRun {result.run_id or '-'}: {result.status} ({summary}; {result.model_calls} model call(s)).")
    if result.message:
        print(result.message)
    print(f"Queue: {store.output_dir / 'queue.json'}   Records: {store.output_dir / 'extractions.jsonl'}")
    if result.status in ("halted", "incomplete"):
        return EXIT_UNFINISHED
    errors = [r for r in store.all_current() if r["status"] == "error"]
    if errors:
        print(f"{len(errors)} document(s) are errors; retry them all with: intake retry --errors")
        return EXIT_ERRORS
    return EXIT_OK


# Commands ------------------------------------------------------------------------------------------

def cmd_run(args, settings: Settings) -> int:
    from .pipeline import Pipeline
    pipeline = Pipeline(settings, progress=_print_outcome)
    print(f"Processing with {pipeline.response_source} (policy {policy.POLICY_VERSION}, prompt {PROMPT_VERSION}).")
    return _finish(pipeline.run(args.paths, force=args.force), pipeline.store)


def cmd_retry(args, settings: Settings) -> int:
    from .pipeline import Pipeline
    if not args.document_ids and not args.errors:
        print("Name documents to retry (intake retry IT-002.json) or use --errors.", file=sys.stderr)
        return EXIT_USAGE
    pipeline = Pipeline(settings, progress=_print_outcome)
    try:
        result = pipeline.retry(args.document_ids, errors=args.errors, force=args.force)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return EXIT_USAGE
    return _finish(result, pipeline.store)


def cmd_resume(args, settings: Settings) -> int:
    from .pipeline import Pipeline
    pipeline = Pipeline(settings, progress=_print_outcome)
    try:
        result = pipeline.resume(args.run_id)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return EXIT_USAGE
    if result.status == "nothing-to-resume":
        print("Nothing to resume: no run has pending, running or deferred documents.")
        return EXIT_OK
    return _finish(result, pipeline.store)


def cmd_status(args, settings: Settings) -> int:
    store = _store(settings)
    runs = store.runs()
    header = None
    if args.run_id:
        try:
            header = store.read_run(args.run_id)
        except KeyError:
            print(f"no run {args.run_id}", file=sys.stderr)
            return EXIT_USAGE
    elif runs:
        header = runs[-1]
    if header is None:
        print("No runs yet. Start one with: intake run materials/requests")
    else:
        states = store.run_states(header["run_id"])
        counts = Counter(e["state"] for e in states.values())
        print(f"Run {header['run_id']}: {header['status']}  (started {header['started_at']}, "
              f"finished {header.get('finished_at') or '-'})")
        print(f"  settings: {header.get('response_source')}, policy {header.get('policy_version')}, "
              f"prompt {header.get('prompt_version')}")
        print("  documents: " + (", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "none"))
        open_docs = sorted(d for d, e in states.items() if e["state"] in OPEN_STATES)
        if open_docs:
            print(f"  open ({len(open_docs)}): {', '.join(open_docs[:20])}{' ...' if len(open_docs) > 20 else ''}")
            print(f"  continue with: intake resume {header['run_id']}")
        if header.get("message"):
            print(f"  note: {header['message']}")
    records = store.all_current()
    print(f"\nCurrent queue: {len(records)} record(s)")
    for (owner, action), n in sorted(Counter((r["owner"], r["next_action"]) for r in records).items()):
        print(f"  {owner:<16} {action:<24} {n}")
    errors = [r for r in records if r["status"] == "error"]
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for r in errors:
            print(f"  {r['document_id']:<16} {r['error']['code']:<24} {r['error']['retry_command']}")
    fingerprint = _current_fingerprint(settings)
    stale = [r["document_id"] for r in records if fingerprint and r.get("settings_fingerprint") != fingerprint]
    if stale:
        print(f"\n{len(stale)} record(s) were produced with other settings or policy than the current configuration; "
              f"run `intake run <exports>` to refresh them: {', '.join(stale[:10])}{' ...' if len(stale) > 10 else ''}")
    return EXIT_OK


def cmd_queue(args, settings: Settings) -> int:
    path = settings.output_dir / "queue.json"
    if not path.is_file():
        print(f"No queue yet at {path}. Start intake with: intake run materials/requests")
        return EXIT_OK
    queue = json.loads(path.read_text(encoding="utf-8"))
    entries = [e for e in queue["entries"]
               if (not args.owner or e["owner"].lower() == args.owner.lower())
               and (not args.status_filter or e["status"] == args.status_filter)
               and (not args.next_action or e["next_action"] == args.next_action)]
    if args.json:
        print(json.dumps(entries, indent=2, ensure_ascii=False))
        return EXIT_OK
    print(f"{'DOCUMENT':<14} {'REQUEST':<8} {'OWNER':<16} {'STATUS':<18} {'NEXT ACTION':<23} {'REQUESTER':<16} "
          f"{'SYSTEM/ENV/PERM':<26} ISSUES")
    for e in entries:
        access = "/".join(str(e.get(k) or "-") for k in ("system", "environment", "permission"))
        notes = f"[{e['error']['code']}]" if e.get("error") else (
            ", ".join(sorted({i.split(':', 1)[0] for i in e.get("issues") or []})) or "-")
        print(f"{e['document_id']:<14} {str(e.get('request_id') or '-'):<8} {e['owner']:<16} {e['status']:<18} "
              f"{e['next_action']:<23} {str(e.get('requester') or '-'):<16} {access:<26} {notes}")
    print(f"\n{len(entries)} of {queue['total']} entries (queue generated {queue['generated_at']}).")
    return EXIT_OK


def cmd_show(args, settings: Settings) -> int:
    store = _store(settings)
    document_id = _norm(args.document_id)
    record = store.read_current(document_id)
    if record is None:
        print(f"No current record for {document_id}.", file=sys.stderr)
        return EXIT_USAGE
    history = store.history(document_id) if args.history else []
    if args.json:
        print(json.dumps({"record": record, **({"history": history} if args.history else {})}, indent=2,
                         ensure_ascii=False))
        return EXIT_OK
    print(f"{record['document_id']}  ({record.get('source_path')}, sha256 {str(record['source_sha256'])[:12]}...)")
    print(f"  queue: {record['owner']} / {record['status']} / {record['next_action']}"
          f"  (policy rule {record.get('policy_rule')}: {'; '.join(record.get('reasons') or [])})")
    extraction = record.get("extraction")
    if extraction:
        for key in ("request_id", "request_date", "kind", "requester", "team", "system", "environment", "permission",
                    "business_purpose", "start_date", "end_date", "manager"):
            print(f"  {key:<17} {extraction[key] if extraction[key] is not None else 'null'}")
        print("  supporting quotes:")
        for quote in extraction["supporting_quotes"]:
            print(f"    \"{quote}\"")
        print("  issues:" + ("" if extraction["issues"] else " none"))
        for issue in extraction["issues"]:
            print(f"    - {issue}")
    if record.get("error"):
        print(f"  error: {record['error']['message']}")
        print(f"  retry: {record['error']['retry_command']}")
    if record.get("last_valid_attempt"):
        last = record["last_valid_attempt"]
        print(f"  last valid result: attempt {last['attempt_id']} (source sha256 {last['source_sha256'][:12]}..., "
              f"{'same' if last['same_source'] else 'earlier'} source version, model {last.get('model_id')})")
    print(f"  produced by: {record.get('response_source')}, model {record.get('model_id')}, "
          f"policy {record.get('policy_version')}, prompt {record.get('prompt_version')}, "
          f"attempt {record.get('attempt_id')}, at {record.get('updated_at')}")
    if args.history:
        print(f"  history ({len(history)} attempt(s), oldest first):")
        for attempt in history:
            detail = (attempt.get("error") or {}).get("code") or (attempt.get("decision") or {}).get("next_action")
            print(f"    {attempt['attempt_id']}  {attempt['kind']:<10} {attempt['outcome']:<7} "
                  f"sha {str(attempt.get('source_sha256'))[:12]}  model {attempt.get('model_id')}  "
                  f"policy {attempt.get('policy_version')}  {detail}")
    return EXIT_OK


def cmd_accuracy(args, settings: Settings) -> int:
    from .accuracy import run_accuracy
    report, text = run_accuracy(settings, args.expected, args.out)
    n = len(report.documents)
    print(f"Response source: {', '.join(report.response_sources)}")
    print(f"Scalar fields exact match: {report.scalar('match')} / {report.scalar_total} "
          f"(omissions {report.scalar('omission')}, factual errors {report.scalar('factual-error')}, "
          f"failed {report.scalar('failed')})")
    print(f"business_purpose {report.purpose['pass']}/{n}, quotes {report.quotes['pass']}/{n}, "
          f"issue codes {report.issues['pass']}/{n}, routes {report.routes['pass']}/{n}")
    for m in report.factual_errors():
        print(f"  factual error: {m.document_id} {m.check}: expected {m.expected!r}, got {m.got!r} {m.note}")
    if args.out:
        print(f"Report written to {args.out}")
    else:
        print("(add --out deliverables/accuracy.md to write the report)")
    perfect = report.scalar("match") == report.scalar_total and not [m for m in report.mismatches]
    return EXIT_OK if perfect else EXIT_ERRORS


def cmd_evidence(args, settings: Settings) -> int:
    from .evidence import write_evidence
    evidence = write_evidence(settings, args.export, args.out)
    outcome = evidence.get("handled_outcome", {})
    route = outcome.get("route", {})
    print(f"Wrote {args.out}: {evidence['configuration']['response_source']}, model "
          f"{evidence['configuration']['model_id']}; reply "
          f"{'accepted' if outcome.get('accepted') else 'not accepted'} -> "
          f"{route.get('owner')} / {route.get('status')} / {route.get('next_action')}")
    return EXIT_OK


COMMANDS = {"run": cmd_run, "status": cmd_status, "queue": cmd_queue, "show": cmd_show, "retry": cmd_retry,
            "resume": cmd_resume, "accuracy": cmd_accuracy, "evidence": cmd_evidence}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logger = get_logger()
    handler, level = None, logger.level
    if args.verbose:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    try:
        settings = _settings(args)
        return COMMANDS[args.command](args, settings)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"intake: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("\nInterrupted. Completed documents are saved; continue with: intake resume", file=sys.stderr)
        return EXIT_INTERRUPTED
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            logger.setLevel(level)


if __name__ == "__main__":
    sys.exit(main())
