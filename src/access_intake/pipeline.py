"""Process a batch of exports: one independent unit of work per document, committed as it finishes.

For each document, in a bounded thread pool (settings.concurrency):
  1. claim it for this run (another run's live claim defers it: two operators never duplicate work)
  2. load the bytes and hash them; a malformed export becomes an error record
  3. unchanged (same source hash and settings fingerprint, not an error) -> skipped, no model call
  4. a stored success for the same source hash and extraction fingerprint -> reused and re-routed
     under the current policy, no model call (a policy-only change takes this path)
  5. otherwise call the model, retrying timeouts, rate limits and unavailability with exponential
     backoff and jitter (a provider Retry-After wins); validate; route; commit
  6. any failure becomes an error record (IT Service Desk / error / retry-extraction) with the exact
     retry command, and the last valid attempt stays in history and is referenced from the record
After breaker_threshold consecutive documents fail with the model service unreachable, the run stops
dispatching and leaves the rest pending for `intake resume`. An interruption leaves the run's event
log showing which documents are done and which are still pending or running; resume continues it.
"""
from __future__ import annotations

import os
import random
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import policy
from .config import Settings, extraction_fingerprint, load_schema, settings_fingerprint
from .issues import Flags
from .llm import ModelClient, ModelResponse, get_client
from .llm.base import ProviderError, ProviderRateLimited
from .llm.mock import MockFixtureMissing
from .loader import LoadError, SourceDocument, discover, load_document
from .logging_redact import get_logger, redact
from .prompt import PROMPT_VERSION, build_request
from .store import OPEN_STATES, Store, iso
from .validate import ExtractionError, retry_command, validate_extraction, validate_response

# Error codes that mean the model service itself is not answering; they count towards the breaker.
SERVICE_DOWN = {"provider-unavailable", "provider-timeout", "provider-rate-limited", "provider-not-configured"}


@dataclass
class DocumentOutcome:
    """What happened to one document in one run."""

    document_id: str
    path: str
    state: str                      # done | reused | skipped | error | deferred | pending
    owner: str | None = None
    status: str | None = None
    next_action: str | None = None
    model_calls: int = 0
    error_code: str | None = None
    attempt_id: str | None = None
    message: str | None = None


@dataclass
class RunResult:
    run_id: str
    status: str                     # completed | incomplete | halted | interrupted | nothing-to-resume
    outcomes: dict[str, DocumentOutcome] = field(default_factory=dict)
    message: str = ""

    @property
    def model_calls(self) -> int:
        return sum(o.model_calls for o in self.outcomes.values())

    def states(self) -> Counter[str]:
        return Counter(o.state for o in self.outcomes.values())


class _CallFailed(Exception):
    def __init__(self, code: str, message: str, calls: int, retryable: bool) -> None:
        super().__init__(message)
        self.code, self.message, self.calls, self.retryable = code, message, calls, retryable


@dataclass
class _RunContext:
    run_id: str
    force: bool
    full_fp: str
    extraction_fp: str
    policy_version: str
    halt: threading.Event = field(default_factory=threading.Event)
    tripped: bool = False
    consecutive_down: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def display_path(path: Path) -> str:
    """A path relative to the working directory when it lies inside it, else absolute."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(resolved)


class Pipeline:
    """Runs intake over export files with one ModelClient, one Store and one set of settings."""

    def __init__(self, settings: Settings, client: ModelClient | None = None, store: Store | None = None, *,
                 sleep: Callable[[float], None] = time.sleep, jitter: Callable[[], float] = random.random,
                 progress: Callable[[DocumentOutcome], None] | None = None) -> None:
        self.settings = settings
        self.client = client if client is not None else get_client(settings)
        self.store = store if store is not None else Store(settings.state_dir, settings.output_dir)
        self.schema, self.schema_sha256 = load_schema(settings.schema_path)
        self.sleep, self.jitter, self.progress = sleep, jitter, progress
        self.response_source = getattr(self.client, "response_source", None) or \
            f"{settings.provider}/{settings.model_id}"
        self.log = get_logger()

    # Fingerprints (read at run time, so a changed POLICY_VERSION is picked up) ------------------

    def fingerprints(self) -> tuple[str, str, str]:
        """(settings fingerprint, extraction fingerprint, policy version) for the current configuration."""
        version = policy.POLICY_VERSION
        full = settings_fingerprint(self.settings, prompt_version=PROMPT_VERSION, policy_version=version,
                                    schema_sha256=self.schema_sha256)
        extraction = extraction_fingerprint(self.settings, prompt_version=PROMPT_VERSION,
                                            schema_sha256=self.schema_sha256)
        return full, extraction, version

    # Entry points --------------------------------------------------------------------------------

    def run(self, paths: Iterable[str | Path], *, force: bool = False, run_id: str | None = None) -> RunResult:
        """Process every export under paths. With run_id, continue that run instead of starting one."""
        files = discover(paths)
        names = Counter(p.name for p in files)
        clashes = sorted(n for n, c in names.items() if c > 1)
        if clashes:
            raise ValueError(f"two inputs share a document_id (filename): {', '.join(clashes)}")
        full_fp, extraction_fp, version = self.fingerprints()
        if run_id is None:
            run_id = self.store.new_run_id()
            self.store.write_run({
                "run_id": run_id, "started_at": iso(self.store.clock()), "finished_at": None, "status": "running",
                "inputs": [display_path(p) for p in files], "settings": self.settings.describe(),
                "settings_fingerprint": full_fp, "extraction_fingerprint": extraction_fp,
                "prompt_version": PROMPT_VERSION, "policy_version": version, "schema_sha256": self.schema_sha256,
                "response_source": self.response_source, "force": force, "pid": os.getpid(),
                "operator": os.environ.get("USER") or os.environ.get("USERNAME"), "resumed": []})
            for path in files:
                self.store.record_event(run_id, path.name, "pending", path=display_path(path))
        else:
            header = self.store.read_run(run_id)
            self.store.update_run(run_id, status="running", finished_at=None,
                                  resumed=[*header.get("resumed", []), {"at": iso(self.store.clock()),
                                                                         "settings_fingerprint": full_fp,
                                                                         "documents": len(files)}])
        context = _RunContext(run_id, force, full_fp, extraction_fp, version)
        result = RunResult(run_id, "running")
        self.log.info("run %s: %d document(s), concurrency %d", run_id, len(files), self.settings.concurrency)
        try:
            with ThreadPoolExecutor(max_workers=self.settings.concurrency, thread_name_prefix="intake") as pool:
                futures = {pool.submit(self._process, path, context): path for path in files}
                try:
                    for future in as_completed(futures):
                        outcome = future.result()
                        result.outcomes[outcome.document_id] = outcome
                        if self.progress:
                            self.progress(outcome)
                except BaseException:
                    context.halt.set()          # stop dispatching; in-flight documents finish or stay open
                    for future in futures:
                        future.cancel()
                    raise
        except BaseException as exc:
            message = f"interrupted ({type(exc).__name__}); continue with: intake resume {run_id}"
            self.store.update_run(run_id, status="interrupted", finished_at=iso(self.store.clock()), message=message)
            self.log.warning("run %s %s", run_id, message)
            raise
        for path in files:   # documents never started because the breaker tripped
            result.outcomes.setdefault(path.name, DocumentOutcome(path.name, display_path(path), "pending"))
        states = result.states()
        if context.tripped:
            result.status = "halted"
            result.message = (f"stopped after {self.settings.breaker_threshold} consecutive documents could not "
                              f"reach the model service; {states['pending']} pending. When it recovers, run: "
                              f"intake resume {run_id}")
        elif states["pending"] or states["deferred"]:
            result.status = "incomplete"
            result.message = (f"{states['deferred']} document(s) were being processed by another run; "
                              f"check `intake status`, then `intake resume {run_id}` if any remain open")
        else:
            result.status = "completed"
        self.store.update_run(run_id, status=result.status, finished_at=iso(self.store.clock()),
                              message=result.message, counts=dict(states), model_calls=result.model_calls)
        self.log.info("run %s %s: %s", run_id, result.status, dict(states))
        return result

    def resume(self, run_id: str | None = None) -> RunResult:
        """Continue a run's open documents (pending, running or deferred). Default: the latest open run."""
        candidates = [self.store.read_run(run_id)] if run_id else list(reversed(self.store.runs()))
        for header in candidates:
            open_paths = [event["path"] for event in self.store.run_states(header["run_id"]).values()
                          if event["state"] in OPEN_STATES]
            if open_paths:
                return self.run(open_paths, run_id=header["run_id"], force=bool(header.get("force")))
            if run_id:
                break
        return RunResult(run_id or "", "nothing-to-resume", message="no run has pending documents")

    def retry(self, document_ids: Iterable[str] = (), *, errors: bool = False, force: bool = False) -> RunResult:
        """Reprocess named documents, or every document whose current record is an error."""
        wanted = [d if d.endswith(".json") else d + ".json" for d in document_ids]
        records = {r["document_id"]: r for r in self.store.all_current()}
        unknown = [d for d in wanted if d not in records]
        if unknown:
            raise KeyError(f"no current record for {', '.join(unknown)}; process it with `intake run <path>`")
        if errors:
            wanted += [d for d, r in records.items() if r["status"] == "error" and d not in wanted]
        if not wanted:
            return RunResult("", "completed", message="nothing to retry")
        return self.run([records[d]["source_path"] for d in wanted], force=force)

    # One document ------------------------------------------------------------------------------

    def _process(self, path: Path, context: _RunContext) -> DocumentOutcome:
        document_id, shown = path.name, display_path(path)
        if context.halt.is_set():
            return DocumentOutcome(document_id, shown, "pending")
        if not self.store.claim(document_id, context.run_id, self.settings.claim_ttl_s):
            holder = (self.store.claim_holder(document_id) or {}).get("run_id")
            self.store.record_event(context.run_id, document_id, "deferred", path=shown, claimed_by=holder)
            return DocumentOutcome(document_id, shown, "deferred", message=f"being processed by run {holder}")
        try:
            self.store.record_event(context.run_id, document_id, "running", path=shown)
            outcome = self._process_claimed(path, shown, context)
            self.store.record_event(context.run_id, document_id, outcome.state, path=shown,
                                    attempt_id=outcome.attempt_id, error_code=outcome.error_code)
            self._update_breaker(outcome, context)
            self.log.info("%s: %s -> %s / %s / %s (%d model call(s))%s", document_id, outcome.state, outcome.owner,
                          outcome.status, outcome.next_action, outcome.model_calls,
                          f" [{outcome.error_code}]" if outcome.error_code else "")
            return outcome
        except BaseException:
            context.halt.set()   # an interruption: no worker starts another document after this one
            raise
        finally:
            self.store.release(document_id, context.run_id)

    def _update_breaker(self, outcome: DocumentOutcome, context: _RunContext) -> None:
        with context.lock:
            if outcome.error_code in SERVICE_DOWN:
                context.consecutive_down += 1
                if context.consecutive_down >= self.settings.breaker_threshold and not context.tripped:
                    context.tripped = True
                    context.halt.set()
                    self.log.warning("run %s: model service unreachable for %d documents in a row; halting",
                                     context.run_id, context.consecutive_down)
            elif outcome.state in ("done", "error"):
                context.consecutive_down = 0

    def _process_claimed(self, path: Path, shown: str, context: _RunContext) -> DocumentOutcome:
        document_id = path.name
        started = iso(self.store.clock())
        try:
            document = load_document(path)
        except LoadError as exc:
            return self._commit_error(document_id, shown, exc.source_sha256, context, started, code=exc.code,
                                      message=f"{exc.code}: {exc.detail}. Fix or re-export the file, then run: "
                                              f"{retry_command(document_id)}", calls=0, retryable=False,
                                      response_source=None)

        current = self.store.read_current(document_id)
        if (not context.force and current and current.get("source_sha256") == document.source_sha256
                and current.get("settings_fingerprint") == context.full_fp and current.get("status") != "error"):
            return DocumentOutcome(document_id, shown, "skipped", current["owner"], current["status"],
                                   current["next_action"], attempt_id=current.get("attempt_id"),
                                   message="unchanged source and settings")

        reusable = None if context.force else self.store.latest_success(document_id, document.source_sha256,
                                                                        context.extraction_fp)
        if reusable is not None:
            return self._commit_reuse(document, shown, reusable, context, started)

        request = build_request(document, self.settings, self.schema)
        try:
            response, calls = self._call(request, context)
        except _CallFailed as failed:
            return self._commit_error(document_id, shown, document.source_sha256, context, started, code=failed.code,
                                      message=failed.message, calls=failed.calls, retryable=failed.retryable,
                                      response_source=self.response_source)
        try:
            valid = validate_response(response, document, self.schema)
        except ExtractionError as exc:
            return self._commit_error(document_id, shown, document.source_sha256, context, started, code=exc.code,
                                      message=str(exc), calls=calls, retryable=exc.retryable,
                                      response_source=response.response_source)
        decision = policy.route(valid.extraction, valid.flags)
        attempt_id = self.store.new_attempt_id(document.source_sha256, context.full_fp)
        flags = _flags_dict(valid.flags)
        attempt = self._attempt_base(document_id, shown, document.source_sha256, attempt_id, context, started,
                                     kind="extraction", outcome="success", calls=calls,
                                     response_source=response.response_source)
        attempt.update(extraction=valid.extraction, flags=flags, added_issues=list(valid.added_issues),
                       added_quotes=list(valid.added_quotes), merged_quotes=list(valid.merged_quotes),
                       decision=decision.as_dict(), error=None,
                       finish_reason=response.finish_reason)
        record = self._record(document_id, shown, document.source_sha256, context, attempt_id=attempt_id,
                              extraction_attempt_id=attempt_id, extraction=valid.extraction, flags=flags,
                              decision=decision, error=None, response_source=response.response_source,
                              calls=calls, last_valid=None)
        self.store.commit(attempt, record)
        return DocumentOutcome(document_id, shown, "done", decision.owner, decision.status, decision.next_action,
                               calls, attempt_id=attempt_id)

    def _call(self, request, context: _RunContext) -> tuple[ModelResponse, int]:
        """One model call with retries for transient provider errors."""
        calls = 0
        while True:
            calls += 1
            try:
                return self.client.complete(request), calls
            except ProviderError as exc:
                last_try = not exc.retryable or calls > self.settings.max_retries or context.halt.is_set()
                if last_try:
                    after = f" after {calls} attempt(s)" if calls > 1 else ""
                    message = (f"{exc.code}: {redact(str(exc))}{after}. Retry with: "
                               f"{retry_command(request.document_id)}")
                    raise _CallFailed(exc.code, message, calls, exc.retryable) from None
                wait = self._backoff(calls, exc)
                self.log.info("%s: %s; retry %d of %d in %.2fs", request.document_id, exc.code, calls,
                              self.settings.max_retries, wait)
                self.sleep(wait)
            except MockFixtureMissing as exc:
                raise _CallFailed("mock-fixture-missing", f"mock-fixture-missing: {exc}. Author a fixture, then: "
                                  f"{retry_command(request.document_id)}", calls, False) from None
            except Exception as exc:   # an unexpected client fault must not stop the batch
                raise _CallFailed("client-error", f"client-error: {type(exc).__name__}: {redact(str(exc))}. Retry "
                                  f"with: {retry_command(request.document_id)}", calls, False) from None

    def _backoff(self, calls: int, exc: ProviderError) -> float:
        if isinstance(exc, ProviderRateLimited) and exc.retry_after is not None:
            return max(0.0, float(exc.retry_after))
        ceiling = min(self.settings.backoff_max_s, self.settings.backoff_base_s * (2 ** (calls - 1)))
        return ceiling * (0.5 + 0.5 * self.jitter())

    # Records ---------------------------------------------------------------------------------

    def _attempt_base(self, document_id: str, shown: str, source_sha256: str | None, attempt_id: str,
                      context: _RunContext, started: str, *, kind: str, outcome: str, calls: int,
                      response_source: str | None) -> dict[str, Any]:
        return {"attempt_id": attempt_id, "kind": kind, "outcome": outcome, "document_id": document_id,
                "source_path": shown, "source_sha256": source_sha256, "run_id": context.run_id,
                "started_at": started, "finished_at": iso(self.store.clock()), "model_calls": calls,
                "response_source": response_source, "provider": self.settings.provider,
                "model_id": self.settings.model_id, "model_params": self.settings.model_params(),
                "settings_fingerprint": context.full_fp, "extraction_fingerprint": context.extraction_fp,
                "prompt_version": PROMPT_VERSION, "policy_version": context.policy_version,
                "schema_sha256": self.schema_sha256}

    def _record(self, document_id: str, shown: str, source_sha256: str | None, context: _RunContext, *,
                attempt_id: str, extraction_attempt_id: str | None, extraction: dict[str, Any] | None,
                flags: dict[str, Any] | None, decision: policy.QueueDecision, error: dict[str, Any] | None,
                response_source: str | None, calls: int, last_valid: dict[str, Any] | None,
                settings_fp: str | None = None, extraction_fp: str | None = None) -> dict[str, Any]:
        """The current record for a document: one line of extractions.jsonl."""
        return {"document_id": document_id, "source_sha256": source_sha256, "extraction": extraction,
                "status": decision.status, "owner": decision.owner, "next_action": decision.next_action,
                "error": error, "response_source": response_source, "source_path": shown,
                "policy_rule": decision.rule, "reasons": list(decision.reasons), "flags": flags,
                "settings_fingerprint": settings_fp or context.full_fp,
                "extraction_fingerprint": extraction_fp or context.extraction_fp,
                "provider": self.settings.provider, "model_id": self.settings.model_id,
                "prompt_version": PROMPT_VERSION, "policy_version": context.policy_version,
                "schema_sha256": self.schema_sha256, "attempt_id": attempt_id,
                "extraction_attempt_id": extraction_attempt_id, "last_valid_attempt": last_valid,
                "model_calls": calls, "run_id": context.run_id, "updated_at": iso(self.store.clock())}

    def _commit_reuse(self, document: SourceDocument, shown: str, reusable: dict[str, Any], context: _RunContext,
                      started: str) -> DocumentOutcome:
        """Re-check and re-route a stored extraction under the current code and policy, without a model call.

        The deterministic checks are idempotent, so running them again on a stored extraction applies any
        check added since it was made (for example the code-level conflict check) and changes nothing else.
        """
        try:
            valid = validate_extraction(reusable["extraction"], document, self.schema)
        except ExtractionError as exc:
            return self._commit_error(document.document_id, shown, document.source_sha256, context, started,
                                      code=exc.code, message=str(exc), calls=0, retryable=False,
                                      response_source=reusable.get("response_source"))
        flags = _flags_dict(valid.flags)
        decision = policy.route(valid.extraction, valid.flags)
        attempt_id = self.store.new_attempt_id(document.source_sha256, context.full_fp)
        attempt = self._attempt_base(document.document_id, shown, document.source_sha256, attempt_id, context,
                                     started, kind="reroute", outcome="success", calls=0,
                                     response_source=reusable.get("response_source"))
        attempt.update(reused_from=reusable["attempt_id"], decision=decision.as_dict(), error=None,
                       extraction=valid.extraction, flags=flags, added_issues=list(valid.added_issues),
                       added_quotes=list(valid.added_quotes), merged_quotes=list(valid.merged_quotes))
        record = self._record(document.document_id, shown, document.source_sha256, context, attempt_id=attempt_id,
                              extraction_attempt_id=reusable["attempt_id"], extraction=valid.extraction,
                              flags=flags, decision=decision, error=None,
                              response_source=reusable.get("response_source"), calls=0, last_valid=None)
        self.store.commit(attempt, record)
        return DocumentOutcome(document.document_id, shown, "reused", decision.owner, decision.status,
                               decision.next_action, 0, attempt_id=attempt_id,
                               message=f"re-routed stored extraction {reusable['attempt_id']}")

    def _commit_error(self, document_id: str, shown: str, source_sha256: str | None, context: _RunContext,
                      started: str, *, code: str, message: str, calls: int, retryable: bool,
                      response_source: str | None) -> DocumentOutcome:
        """Record a failed attempt; the current record shows the error and points at the last success."""
        message = redact(message)
        error = {"code": code, "message": message, "retry_command": retry_command(document_id),
                 "retryable": retryable, "model_calls": calls}
        attempt_id = self.store.new_attempt_id(source_sha256, context.full_fp)
        attempt = self._attempt_base(document_id, shown, source_sha256, attempt_id, context, started,
                                     kind="extraction" if code != LoadError.code else "load", outcome="error",
                                     calls=calls, response_source=response_source)
        attempt.update(extraction=None, flags=None, error=error, decision=policy.route_error().as_dict())
        previous = self.store.latest_success(document_id)
        last_valid = None if previous is None else {
            "attempt_id": previous["attempt_id"], "source_sha256": previous["source_sha256"],
            "settings_fingerprint": previous["settings_fingerprint"], "model_id": previous.get("model_id"),
            "finished_at": previous["finished_at"], "same_source": previous["source_sha256"] == source_sha256}
        record = self._record(document_id, shown, source_sha256, context, attempt_id=attempt_id,
                              extraction_attempt_id=None, extraction=None, flags=None,
                              decision=policy.route_error(), error=error, response_source=response_source,
                              calls=calls, last_valid=last_valid)
        self.store.commit(attempt, record)
        return DocumentOutcome(document_id, shown, "error", record["owner"], record["status"], record["next_action"],
                               calls, error_code=code, attempt_id=attempt_id, message=message)


def _flags_dict(flags: Flags) -> dict[str, Any]:
    return {"injection": flags.injection, "injection_evidence": list(flags.injection_evidence),
            "id_mismatch": flags.id_mismatch, "date_order": flags.date_order, "conflicts": list(flags.conflicts)}


def _flags_from(data: dict[str, Any]) -> Flags:
    return Flags(injection=bool(data.get("injection")), injection_evidence=tuple(data.get("injection_evidence") or ()),
                 id_mismatch=bool(data.get("id_mismatch")), date_order=bool(data.get("date_order")),
                 conflicts=tuple(data.get("conflicts") or ()))
