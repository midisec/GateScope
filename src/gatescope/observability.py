"""Small structured logger with mandatory recursive secret redaction."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Mapping

SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|authorization|cookie|password|secret|session|token)", re.I)
SENSITIVE_VALUE = re.compile(r"(?i)(bearer\s+)[^\s]+|\bcg_[A-Za-z0-9_-]{12,}\b|([a-z][a-z0-9+.-]*://)[^/@\s]+@")


def redact(value: Any, key: str = "") -> Any:
    if key and SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = SENSITIVE_VALUE.sub(lambda m: (m.group(1) if m.group(1) else "") + "[REDACTED]", value)
        return value[:2000]
    return value


def event(name: str, *, level: int = logging.INFO, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": name,
        **redact(fields),
    }
    logging.getLogger("gatescope").log(level, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

