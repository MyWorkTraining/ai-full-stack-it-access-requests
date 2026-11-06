"""Credential redaction for operational logs and stored error text.

redact() removes the current API_KEY value and common credential shapes (Authorization headers,
bearer tokens, sk-/dapi-style keys, api_key=... pairs) from any text. RedactingFilter applies it to
every log record of the access_intake logger, including formatted arguments and exception text.
"""
from __future__ import annotations

import logging
import os
import re

from .config import API_KEY_ENV

REDACTED = "[REDACTED]"
LOGGER_NAME = "access_intake"

_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;\"']+"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|x-api-key|token|secret|password)\s*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+"),
    re.compile(r"()\b(?:sk-|pk-|rk-|dapi)[A-Za-z0-9_-]{8,}\b"),
]


def redact(text: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """The text with the API_KEY value, any extra secrets and credential-shaped strings replaced."""
    if not text:
        return text
    secrets = [s for s in (os.environ.get(API_KEY_ENV, ""), *extra_secrets) if s and len(s) >= 4]
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Redacts each record's message, arguments and exception text before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def get_logger() -> logging.Logger:
    """The package logger, with the redaction filter installed once."""
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(f, RedactingFilter) for f in logger.filters):
        logger.addFilter(RedactingFilter())
    return logger
