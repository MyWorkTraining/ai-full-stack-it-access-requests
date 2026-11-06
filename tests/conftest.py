"""Shared test fixtures: project paths, the schema, the expected truth and the mock client."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from access_intake.config import Settings  # noqa: E402
from access_intake.llm import MockModelClient, ModelRequest  # noqa: E402
from access_intake.loader import SourceDocument, load_document  # noqa: E402
from access_intake.prompt import build_request  # noqa: E402
from access_intake.pipeline import Pipeline  # noqa: E402

REQUESTS_DIR = ROOT / "materials" / "requests"
SYNTHETIC_DIR = ROOT / "tests" / "fixtures" / "synthetic"
SCHEMA_PATH = ROOT / "snapshot.schema.json"
MOCK_RESPONSES = ROOT / "tests" / "fixtures" / "mock_responses.json"
EXPECTED_PATH = ROOT / "tests" / "expected" / "expected_fields.json"

EXPECTED = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
EXPECTED_DOCUMENTS: dict = EXPECTED["documents"]
SCALAR_FIELDS: list[str] = EXPECTED["scalar_fields"]
ALL_DOCUMENT_IDS = sorted(EXPECTED_DOCUMENTS)          # the 20 supplied messages + IT-101
SUPPLIED_IDS = sorted(p.name for p in REQUESTS_DIR.glob("*.json"))


def source_path(document_id: str) -> Path:
    """The export file for a document in the expected truth."""
    return ROOT / EXPECTED_DOCUMENTS[document_id]["source"]


def make_request(document: SourceDocument, schema: dict | None = None, model_id: str = "mock-extractor-v1") -> ModelRequest:
    """A ModelRequest for a document, built exactly as the pipeline builds it (the real prompt)."""
    if schema is None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return build_request(document, Settings(model_id=model_id), schema)


@pytest.fixture(scope="session")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def mock_client() -> MockModelClient:
    """A fresh mock client over the committed fixtures, with no faults."""
    return MockModelClient(MOCK_RESPONSES)


@pytest.fixture
def load():
    """Load a document by id from the expected truth's source path."""
    return lambda document_id: load_document(source_path(document_id))


# Pipeline helpers ---------------------------------------------------------------------------------

ALL_SOURCES = [REQUESTS_DIR, SYNTHETIC_DIR / "IT-101.json"]     # the 20 supplied exports + the paraphrase


def make_settings(base: Path, **changes) -> Settings:
    """Settings writing under base, using the committed schema and fixtures, with tiny backoff waits."""
    values = dict(state_dir=base / "state", output_dir=base / "out", schema_path=SCHEMA_PATH,
                  mock_responses=MOCK_RESPONSES, backoff_base_s=0.01, backoff_max_s=0.1)
    values.update(changes)
    return Settings(**values)


class Sleeps(list):
    """A stand-in for time.sleep that records each requested wait instead of waiting."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
def make_pipeline(tmp_path, sleeps):
    """Build a Pipeline over tmp_path: make_pipeline(client=None, faults=None, store=None, **settings)."""
    def factory(client=None, faults=None, store=None, **changes) -> Pipeline:
        settings = make_settings(tmp_path, **changes)
        if client is None and settings.provider == "mock":
            client = MockModelClient(MOCK_RESPONSES, faults=faults)
        return Pipeline(settings, client, store, sleep=sleeps, jitter=lambda: 1.0)
    return factory


def read_outputs(base: Path) -> tuple[dict[str, dict], dict]:
    """({document_id: extractions.jsonl record}, queue.json) under base/out."""
    lines = (base / "out" / "extractions.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == len({r["document_id"] for r in records}), "one record per document"
    queue = json.loads((base / "out" / "queue.json").read_text(encoding="utf-8"))
    return {r["document_id"]: r for r in records}, queue


def history_files(base: Path) -> list[Path]:
    """Every stored attempt file (the per-document folders are named after the document, e.g. IT-001.json)."""
    return sorted(p for p in (base / "state" / "history").rglob("*.json") if p.is_file())
