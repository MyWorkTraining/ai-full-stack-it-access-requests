"""A deterministic stand-in for the language model, for development and tests.

Replies come from a fixtures file (default tests/fixtures/mock_responses.json):

    {"fixture_version": 1,
     "documents": {"IT-001.json": {"source": "materials/requests/IT-001.json",
                                    "source_sha256": "<sha256 of the source bytes>",
                                    "extraction": {...one snapshot.schema.json object...}}, ...}}

A fixture is pinned to its source hash: a request whose source_sha256 differs gets the fixture
added for that hash (add_fixture), or MockFixtureMissing. So changed source content can never
silently reuse a reply written for the old content.

Fault injection simulates provider behaviour per document, from the constructor or the
MOCK_FAULTS setting, e.g. "IT-002:unavailable,IT-003:timeout*1,*:rate_limit*1":

    timeout, rate_limit, unavailable, bad_request   raise the matching ProviderError
    malformed          reply text that is not valid JSON (finish_reason stop)
    schema_violation   valid JSON of the wrong shape
    refusal            no text, finish_reason refusal
    truncated          half the JSON text, finish_reason length (an interrupted response)
    content_filter     no text, finish_reason content_filter
    ungrounded_quote   the fixture plus a supporting quote that is not in the message
    fail_after_success the first call succeeds, every later call raises ProviderUnavailable

A "*N" suffix applies the fault to the first N calls for that document only, after which the
fixture is returned (to test retry-then-succeed). The document key is a filename ("IT-002.json"),
its stem ("IT-002") or "*" for every document.
"""
from __future__ import annotations

import copy
import json
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import (ModelRequest, ModelResponse, ProviderBadRequest, ProviderRateLimited, ProviderTimeout,
                   ProviderUnavailable)

RESPONSE_SOURCE = "mock"
FAULT_KINDS = ("timeout", "rate_limit", "unavailable", "bad_request", "malformed", "schema_violation",
               "refusal", "truncated", "content_filter", "ungrounded_quote", "fail_after_success")
UNGROUNDED_QUOTE = "This sentence does not appear in the message."


class MockFixtureMissing(LookupError):
    """No fixture exists for this document and source hash. Author one for changed content."""


@dataclass(frozen=True)
class Fault:
    kind: str
    calls: int | None = None   # None: every call; N: the first N calls only

    def applies(self, call_number: int) -> bool:
        """Whether this fault fires on the call_number-th call (from 1) for its document."""
        if self.kind == "fail_after_success":
            return call_number > 1
        return self.calls is None or call_number <= self.calls


def _key(document: str) -> str:
    """Normalise a document key: "IT-002" and "IT-002.json" are the same document."""
    return document if document == "*" or document.endswith(".json") else document + ".json"


def parse_faults(spec: str | dict[str, str | Fault] | None) -> dict[str, Fault]:
    """Parse a fault spec ("IT-002:unavailable,IT-003:timeout*1") or a dict into {document_id: Fault}."""
    if not spec:
        return {}
    items = spec.items() if isinstance(spec, dict) else (
        part.strip().split(":", 1) for part in spec.split(",") if part.strip())
    faults: dict[str, Fault] = {}
    for item in items:
        if len(item) != 2:
            raise ValueError(f"fault must look like DOCUMENT:KIND[*N], got {':'.join(item)!r}")
        document, value = item
        if isinstance(value, Fault):
            fault = value
        else:
            kind, _, count = value.strip().partition("*")
            fault = Fault(kind, int(count) if count else None)
        if fault.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault {fault.kind!r}; known: {', '.join(FAULT_KINDS)}")
        faults[_key(document.strip())] = fault
    return faults


def load_fixtures(path: str | Path) -> dict[str, dict[str, Any]]:
    """The fixtures file's documents: {document_id: {"source", "source_sha256", "extraction"}}."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    documents = data.get("documents")
    if not isinstance(documents, dict):
        raise ValueError(f"{path}: expected a 'documents' object")
    return documents


class MockModelClient:
    """A ModelClient that answers from fixtures, with optional fault injection. Thread-safe.

    Attributes:
        requests: every ModelRequest received, in order (for evidence and call-count assertions).
    """

    response_source = RESPONSE_SOURCE

    def __init__(self, fixtures_path: str | Path | None = None, *,
                 fixtures: dict[str, dict[str, Any]] | None = None,
                 faults: str | dict[str, str | Fault] | None = None) -> None:
        if fixtures is None:
            if fixtures_path is None:
                raise ValueError("give fixtures_path or fixtures")
            fixtures = load_fixtures(fixtures_path)
        self._by_document: dict[str, dict[str, Any]] = {}
        self._by_hash: dict[tuple[str, str], dict[str, Any]] = {}
        for document_id, entry in fixtures.items():
            self.add_fixture(document_id, entry.get("source_sha256"), entry["extraction"])
        self.faults = parse_faults(faults)
        self.requests: list[ModelRequest] = []
        self._calls: Counter[str] = Counter()
        self._lock = threading.Lock()

    def add_fixture(self, document_id: str, source_sha256: str | None, extraction: dict[str, Any]) -> None:
        """Register a reply for a document; with a hash, only for that version of its content."""
        entry = {"source_sha256": source_sha256, "extraction": copy.deepcopy(extraction)}
        if source_sha256:
            self._by_hash[(document_id, source_sha256)] = entry
        self._by_document.setdefault(document_id, entry)

    def set_fault(self, document_id: str, kind: str, calls: int | None = None) -> None:
        """Add or replace the fault for one document (or "*")."""
        self.faults.update(parse_faults({document_id: Fault(kind, calls)}))

    def call_count(self, document_id: str | None = None) -> int:
        """Calls received for one document, or in total."""
        with self._lock:
            return self._calls[document_id] if document_id else sum(self._calls.values())

    def extraction_for(self, request: ModelRequest) -> dict[str, Any]:
        """The fixture extraction for this document and source version."""
        pinned = self._by_hash.get((request.document_id, request.source_sha256))
        if pinned:
            return copy.deepcopy(pinned["extraction"])
        entry = self._by_document.get(request.document_id)
        if entry is None:
            raise MockFixtureMissing(f"no mock fixture for {request.document_id}")
        if entry["source_sha256"] and entry["source_sha256"] != request.source_sha256:
            raise MockFixtureMissing(
                f"mock fixture for {request.document_id} was written for source {entry['source_sha256'][:12]}, "
                f"not {request.source_sha256[:12]}; add a fixture for the changed content")
        return copy.deepcopy(entry["extraction"])

    def complete(self, request: ModelRequest) -> ModelResponse:
        with self._lock:
            self.requests.append(request)
            self._calls[request.document_id] += 1
            call_number = self._calls[request.document_id]
        fault = self.faults.get(request.document_id) or self.faults.get("*")
        kind = fault.kind if fault and fault.applies(call_number) else None
        raw = {"fixture": request.document_id, "call": call_number, "fault": kind}

        if kind == "timeout":
            raise ProviderTimeout(f"mock: no reply for {request.document_id} within the timeout")
        if kind == "rate_limit":
            raise ProviderRateLimited(f"mock: rate limited for {request.document_id}", retry_after=0.0)
        if kind in ("unavailable", "fail_after_success"):
            raise ProviderUnavailable(f"mock: model service unavailable for {request.document_id}")
        if kind == "bad_request":
            raise ProviderBadRequest(f"mock: request for {request.document_id} rejected")
        if kind == "refusal":
            return ModelResponse(None, "refusal", RESPONSE_SOURCE, refusal="mock: I can't help with that.", raw=raw)
        if kind == "content_filter":
            return ModelResponse(None, "content_filter", RESPONSE_SOURCE, raw=raw)
        if kind == "schema_violation":
            return ModelResponse(json.dumps({"request_id": 1, "unexpected": True}), "stop", RESPONSE_SOURCE, raw=raw)

        extraction = self.extraction_for(request)
        if kind == "ungrounded_quote":
            extraction["supporting_quotes"] = [*extraction["supporting_quotes"], UNGROUNDED_QUOTE]
        text = json.dumps(extraction, ensure_ascii=False)
        if kind == "malformed":
            return ModelResponse("Here is the record: " + text[: len(text) // 3], "stop", RESPONSE_SOURCE, raw=raw)
        if kind == "truncated":
            return ModelResponse(text[: len(text) // 2], "length", RESPONSE_SOURCE, raw=raw)
        return ModelResponse(text, "stop", RESPONSE_SOURCE, raw=raw)
