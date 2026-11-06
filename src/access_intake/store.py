"""File-based state: immutable attempt history, current records, run manifests, claims and outputs.

    state/
    ├── history/<document_id>/<attempt_id>.json   every attempt (success, error or re-route); never changed
    ├── current/<document_id>                     the latest attempt's record, as written to extractions.jsonl
    ├── runs/<run_id>.json                        run header: inputs, settings, status
    ├── runs/<run_id>.events.jsonl                append-only per-document state changes
    ├── claims/<document_id>                      which run is processing a document now
    └── intake.lock                               the writers' lock
    <output_dir>/extractions.jsonl, queue.json    regenerated from current/ after every commit

Every write is atomic (temporary file, fsync, os.replace) and happens under one lock: an OS file lock
across processes (fcntl.flock; msvcrt on Windows) plus a re-entrant thread lock inside a process. A
crash therefore leaves each file either old or new, never half-written, and each document's commit
(history, current, outputs) is complete before the next document's begins, so extractions.jsonl and
queue.json always agree on document_id and source_sha256. Prior versions live only in history/,
separately from the current outputs.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

try:   # POSIX
    import fcntl
except ImportError:   # pragma: no cover - Windows
    fcntl = None
    import msvcrt

FINAL_STATES = ("done", "error", "skipped", "reused")
OPEN_STATES = ("pending", "running", "deferred")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_id(document_id: str) -> str:
    if not document_id or "/" in document_id or "\\" in document_id or document_id in (".", ".."):
        raise ValueError(f"unsafe document_id {document_id!r}")
    return document_id


def atomic_write(path: Path, text: str) -> None:
    """Write text to path so that readers see either the old or the new file, never a partial one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    with contextlib.suppress(OSError):   # persist the rename itself (not supported everywhere)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class _FileLock:
    """An exclusive lock on a file, held between acquire() and release()."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:   # pragma: no cover - Windows
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        self._fd = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:   # pragma: no cover - Windows
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


class Store:
    """The intake's persistent state under state_dir, and its published outputs under output_dir."""

    def __init__(self, state_dir: str | Path, output_dir: str | Path,
                 clock: Callable[[], dt.datetime] = utc_now) -> None:
        self.state_dir = Path(state_dir)
        self.output_dir = Path(output_dir)
        self.clock = clock
        self._thread_lock = threading.RLock()
        self._depth = 0
        self._file_lock = _FileLock(self.state_dir / "intake.lock")
        for name in ("history", "current", "runs", "claims"):
            (self.state_dir / name).mkdir(parents=True, exist_ok=True)

    # Locking ---------------------------------------------------------------------------------

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the writers' lock (re-entrant within this Store)."""
        with self._thread_lock:
            if self._depth == 0:
                self._file_lock.acquire()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._file_lock.release()

    # Current records and history -------------------------------------------------------------

    def _current_path(self, document_id: str) -> Path:
        return self.state_dir / "current" / _safe_id(document_id)   # document_id is already a .json filename

    def _history_dir(self, document_id: str) -> Path:
        return self.state_dir / "history" / _safe_id(document_id)

    def new_attempt_id(self, source_sha256: str | None, fingerprint: str) -> str:
        """A unique, chronologically sortable attempt id: time, source hash, settings fingerprint."""
        stamp = self.clock().astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{stamp}_{(source_sha256 or 'nohash')[:12]}_{fingerprint[:12]}_{uuid.uuid4().hex[:6]}"

    def read_current(self, document_id: str) -> dict[str, Any] | None:
        path = self._current_path(document_id)
        return read_json(path) if path.is_file() else None

    def all_current(self) -> list[dict[str, Any]]:
        """Every current record, sorted by document_id."""
        records = [read_json(p) for p in (self.state_dir / "current").iterdir()
                   if p.is_file() and not p.name.startswith(".")]
        return sorted(records, key=lambda r: r["document_id"])

    def history(self, document_id: str) -> list[dict[str, Any]]:
        """Every stored attempt for a document, oldest first."""
        folder = self._history_dir(document_id)
        if not folder.is_dir():
            return []
        return [read_json(p) for p in sorted(folder.glob("*.json"))]

    def latest_success(self, document_id: str, source_sha256: str | None = None,
                       extraction_fingerprint: str | None = None) -> dict[str, Any] | None:
        """The newest successful extraction attempt, optionally for one source version and settings."""
        for attempt in reversed(self.history(document_id)):
            if attempt.get("kind") != "extraction" or attempt.get("outcome") != "success":
                continue
            if source_sha256 is not None and attempt.get("source_sha256") != source_sha256:
                continue
            if extraction_fingerprint is not None and attempt.get("extraction_fingerprint") != extraction_fingerprint:
                continue
            return attempt
        return None

    def commit(self, attempt: dict[str, Any] | None, current: dict[str, Any]) -> None:
        """Store one attempt (if any) and the document's new current record, then republish outputs."""
        with self.locked():
            if attempt is not None:
                path = self._history_dir(attempt["document_id"]) / f"{attempt['attempt_id']}.json"
                if path.exists():
                    raise FileExistsError(f"attempt {attempt['attempt_id']} is already stored")
                write_json(path, attempt)
            write_json(self._current_path(current["document_id"]), current)
            self.write_outputs()

    # Published outputs -------------------------------------------------------------------------

    def write_outputs(self) -> None:
        """Regenerate extractions.jsonl and queue.json from the current records."""
        with self.locked():
            records = self.all_current()
            lines = "".join(json.dumps(r, ensure_ascii=False, sort_keys=False) + "\n" for r in records)
            atomic_write(self.output_dir / "extractions.jsonl", lines)
            write_json(self.output_dir / "queue.json", build_queue(records, iso(self.clock())))

    # Claims ------------------------------------------------------------------------------------

    def claim(self, document_id: str, run_id: str, ttl_s: float) -> bool:
        """Claim a document for a run. False if another run holds a claim younger than ttl_s."""
        path = self._claim_path(document_id)
        with self.locked():
            now = time.time()
            if path.is_file():
                held = read_json(path)
                if held.get("run_id") != run_id and now - float(held.get("claimed_at", 0)) < ttl_s:
                    return False
            write_json(path, {"document_id": document_id, "run_id": run_id, "claimed_at": now,
                              "pid": os.getpid()})
            return True

    def _claim_path(self, document_id: str) -> Path:
        return self.state_dir / "claims" / _safe_id(document_id)

    def release(self, document_id: str, run_id: str) -> None:
        path = self._claim_path(document_id)
        with self.locked():
            if path.is_file() and read_json(path).get("run_id") == run_id:
                path.unlink()

    def claim_holder(self, document_id: str) -> dict[str, Any] | None:
        path = self._claim_path(document_id)
        return read_json(path) if path.is_file() else None

    # Runs --------------------------------------------------------------------------------------

    def new_run_id(self) -> str:
        return self.clock().astimezone(dt.timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]

    def write_run(self, header: dict[str, Any]) -> None:
        with self.locked():
            write_json(self.state_dir / "runs" / f"{header['run_id']}.json", header)

    def update_run(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with self.locked():
            header = {**self.read_run(run_id), **changes}
            self.write_run(header)
            return header

    def read_run(self, run_id: str) -> dict[str, Any]:
        path = self.state_dir / "runs" / f"{run_id}.json"
        if not path.is_file():
            raise KeyError(f"no run {run_id}")
        return read_json(path)

    def runs(self) -> list[dict[str, Any]]:
        """Every run header, oldest first."""
        headers = [read_json(p) for p in (self.state_dir / "runs").glob("*.json")]
        return sorted(headers, key=lambda h: (h["started_at"], h["run_id"]))

    def record_event(self, run_id: str, document_id: str, state: str, **details: Any) -> None:
        """Append one document state change to the run's event log (pending, running, done, ...)."""
        event = {"at": iso(self.clock()), "document_id": document_id, "state": state, **details}
        path = self.state_dir / "runs" / f"{run_id}.events.jsonl"
        with self.locked():
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def run_states(self, run_id: str) -> dict[str, dict[str, Any]]:
        """{document_id: its latest event} for a run. A torn last line (crash mid-append) is ignored."""
        path = self.state_dir / "runs" / f"{run_id}.events.jsonl"
        states: dict[str, dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                states[event["document_id"]] = event
        return states


def build_queue(records: list[dict[str, Any]], generated_at: str) -> dict[str, Any]:
    """The review queue: one entry per current record, linked to extractions.jsonl by document_id."""
    entries = []
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        extraction = record.get("extraction") or {}
        entries.append({
            "document_id": record["document_id"],
            "source_sha256": record["source_sha256"],
            "owner": record["owner"],
            "status": record["status"],
            "next_action": record["next_action"],
            "request_id": extraction.get("request_id"),
            "kind": extraction.get("kind"),
            "requester": extraction.get("requester"),
            "system": extraction.get("system"),
            "environment": extraction.get("environment"),
            "permission": extraction.get("permission"),
            "issues": extraction.get("issues", []),
            "policy_rule": record.get("policy_rule"),
            "reasons": record.get("reasons", []),
            "error": record.get("error"),
            "attempt_id": record.get("attempt_id"),
            "updated_at": record.get("updated_at"),
        })
        counts.setdefault(record["owner"], {}).setdefault(record["next_action"], 0)
        counts[record["owner"]][record["next_action"]] += 1
    return {"generated_at": generated_at, "total": len(entries),
            "policy_versions": sorted({r.get("policy_version") for r in records if r.get("policy_version")}),
            "counts": counts, "entries": entries}
