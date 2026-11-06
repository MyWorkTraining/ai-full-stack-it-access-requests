"""Read message exports: original bytes, SHA-256, document identity and the message text.

Policy: the source hash is the SHA-256 of the original file bytes, document_id is the input
filename, and request facts come only from the decoded `message` string. The export metadata
(export_version, exported_at, source_channel, subject) is kept for display, never as facts.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DISPLAY_FIELDS = ("export_version", "exported_at", "source_channel", "subject")


class LoadError(Exception):
    """An export could not be read as a message. Affects only its own document."""

    code = "malformed-export"

    def __init__(self, document_id: str, detail: str, source_sha256: str | None = None) -> None:
        super().__init__(f"{document_id}: {detail}")
        self.document_id = document_id
        self.detail = detail
        self.source_sha256 = source_sha256


@dataclass(frozen=True)
class SourceDocument:
    """One exported message, as read from disk."""

    document_id: str          # the input filename, e.g. "IT-016.json"
    path: Path
    source_sha256: str        # SHA-256 of the original bytes
    message: str              # the only source of request facts
    export_version: str | None = None
    exported_at: str | None = None
    source_channel: str | None = None
    subject: str | None = None

    @property
    def stem(self) -> str:
        """The filename without its extension, e.g. "IT-016": the identifier the filename states."""
        return Path(self.document_id).stem


def sha256_bytes(raw: bytes) -> str:
    """The hex SHA-256 of some bytes."""
    return hashlib.sha256(raw).hexdigest()


def load_document(path: str | Path) -> SourceDocument:
    """Read one export file.

    Raises:
        LoadError: the file cannot be read, is not UTF-8 JSON, is not an object, or has no
            string `message`. source_sha256 is set whenever the bytes were read.
    """
    path = Path(path)
    document_id = path.name
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LoadError(document_id, f"cannot read file ({exc.strerror or exc})") from exc
    digest = sha256_bytes(raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise LoadError(document_id, "file is not UTF-8", digest) from exc
    except json.JSONDecodeError as exc:
        raise LoadError(document_id, f"invalid JSON at line {exc.lineno} column {exc.colno}", digest) from exc
    if not isinstance(data, dict):
        raise LoadError(document_id, "export is not a JSON object", digest)
    message = data.get("message")
    if not isinstance(message, str):
        raise LoadError(document_id, "export has no string 'message' field", digest)
    if not message.strip():
        raise LoadError(document_id, "export 'message' is empty", digest)
    display = {k: data[k] if isinstance(data.get(k), str) else None for k in DISPLAY_FIELDS}
    return SourceDocument(document_id=document_id, path=path, source_sha256=digest, message=message, **display)


def discover(paths: Iterable[str | Path]) -> list[Path]:
    """The export files named by paths: each directory contributes its *.json files.

    Returns a sorted list without duplicates. A path that does not exist is an error, so a
    mistyped batch folder is not mistaken for an empty batch.
    """
    found: set[Path] = set()
    for item in paths:
        item = Path(item)
        if item.is_dir():
            found.update(p for p in item.glob("*.json") if p.is_file())
        elif item.is_file():
            found.add(item)
        else:
            raise FileNotFoundError(f"no such file or directory: {item}")
    return sorted(found, key=lambda p: (p.name, str(p)))
