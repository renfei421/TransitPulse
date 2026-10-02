"""Structured JSON logs with request/run IDs and conservative credential redaction."""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys

run_id = ContextVar("run_id", default=None)
_SENSITIVE = re.compile(r"password|secret|token|authorization|api[_-]?key", re.I)
_INLINE = re.compile(r"(?i)(password|token|api[_-]?key|authorization)([=:]\s*)([^\s&,;]+)")


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if _SENSITIVE.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _INLINE.sub(r"\1\2[REDACTED]", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
            "logger": record.name, "event": record.getMessage(), "run_id": run_id.get(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            # Exception text can contain URLs/credentials; keep type and an opaque
            # run ID here. The caller records a safe operational error code.
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(redact(payload), ensure_ascii=False, default=str, allow_nan=False)


def configure():
    logger = logging.getLogger("transport")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.propagate = False
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    return logger


def get_logger(component):
    configure()
    return logging.getLogger(f"transport.{component}")
