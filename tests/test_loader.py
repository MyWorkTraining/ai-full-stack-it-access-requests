"""Loader: original-byte hashes, filename identity, message decoding and malformed exports."""
import hashlib
import json

import pytest

from access_intake.loader import LoadError, discover, load_document
from conftest import ALL_DOCUMENT_IDS, REQUESTS_DIR, SUPPLIED_IDS, source_path


@pytest.mark.parametrize("document_id", ALL_DOCUMENT_IDS)
def test_every_example_loads_with_identity_and_hash(document_id):
    path = source_path(document_id)
    document = load_document(path)
    assert document.document_id == document_id == path.name
    assert document.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert document.message == json.loads(path.read_bytes())["message"]
    assert document.subject  # kept for display


def test_the_supplied_batch_is_the_twenty_exports():
    assert SUPPLIED_IDS == [f"IT-{n:03d}.json" for n in range(1, 21)]
    assert [p.name for p in discover([REQUESTS_DIR])] == SUPPLIED_IDS


def test_hash_is_of_the_original_bytes_not_the_parsed_json(tmp_path):
    original = source_path("IT-001.json").read_bytes()
    reformatted = json.dumps(json.loads(original), separators=(",", ":")).encode()
    (tmp_path / "IT-001.json").write_bytes(reformatted)
    copy = load_document(tmp_path / "IT-001.json")
    assert copy.message == load_document(source_path("IT-001.json")).message
    assert copy.source_sha256 != load_document(source_path("IT-001.json")).source_sha256


def test_document_id_is_the_filename_not_the_stated_request_id():
    document = load_document(source_path("IT-016.json"))
    assert document.document_id == "IT-016.json"
    assert document.stem == "IT-016"
    assert "IT-061" in document.message


@pytest.mark.parametrize("content, detail", [
    (b"{not json", "invalid JSON"),
    (b"[1, 2]", "not a JSON object"),
    (b'{"subject": "no message"}', "no string 'message'"),
    (b'{"message": 42}', "no string 'message'"),
    (b'{"message": "   "}', "empty"),
    (b'{"message": "caf\xe9"}', "not UTF-8"),
])
def test_malformed_exports_raise_load_error_with_hash(tmp_path, content, detail):
    path = tmp_path / "IT-999.json"
    path.write_bytes(content)
    with pytest.raises(LoadError) as caught:
        load_document(path)
    assert caught.value.code == "malformed-export"
    assert caught.value.document_id == "IT-999.json"
    assert detail in caught.value.detail
    assert caught.value.source_sha256 == hashlib.sha256(content).hexdigest()


def test_unreadable_file_raises_load_error_without_hash(tmp_path):
    with pytest.raises(LoadError) as caught:
        load_document(tmp_path / "IT-404.json")
    assert caught.value.source_sha256 is None


def test_discover_sorts_dedupes_and_rejects_missing_paths(tmp_path):
    for name in ("b.json", "a.json", "notes.txt"):
        (tmp_path / name).write_text("{}")
    found = discover([tmp_path, tmp_path / "a.json"])
    assert [p.name for p in found] == ["a.json", "b.json"]
    with pytest.raises(FileNotFoundError):
        discover([tmp_path / "missing"])
