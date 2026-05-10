"""Harden httpx/httpcore log lines (tokens in DEBUG traces, secrets in INFO URLs)."""

from __future__ import annotations

import logging
import re

_RE_HTTP_LOG_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_RE_HTTP_LOG_BEARER_BYTES = re.compile(r"b'Bearer[^']*'")
_RE_HTTP_LOG_QUERY_SECRET = re.compile(
    r"([?&])(api[_-]?key|access[_-]?token)\s*=\s*([^&#\s]*)",
    re.I,
)


def redact_http_client_log_text(text: str) -> str:
    redacted = _RE_HTTP_LOG_BEARER.sub("Bearer ***", text)
    redacted = _RE_HTTP_LOG_BEARER_BYTES.sub("b'Bearer ***'", redacted)
    redacted = _RE_HTTP_LOG_QUERY_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}=***", redacted)
    return redacted


class RedactHttpClientSecretsFilter(logging.Filter):
    """Strip bearer tokens / sensitive query params from httpx/httpcore log records."""

    _LOGGER_PREFIXES = ("httpx", "httpcore")

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith(self._LOGGER_PREFIXES):
            return True
        try:
            merged = record.getMessage()
        except Exception:
            return True
        record.msg = redact_http_client_log_text(merged)
        record.args = ()
        return True


def configure_http_client_log_redaction(root: logging.Logger | None = None) -> None:
    """Attach redaction to root handlers; set httpx/httpcore default level to WARNING."""
    root = root or logging.root
    filt = RedactHttpClientSecretsFilter()
    for handler in root.handlers:
        handler.addFilter(filt)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
