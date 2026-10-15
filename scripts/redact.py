"""Strip the Speediance token from anything that might be logged or stored."""

from __future__ import annotations

import os
import re
from typing import Any

_SENSITIVE_KEY = re.compile(r"token|password|secret|authorization|cookie", re.IGNORECASE)


def redact(text: str) -> str:
    token = os.environ.get("SPEEDIANCE_TOKEN") or ""
    if len(token) >= 8 and token in text:
        text = text.replace(token, "[redacted]")
    return text


def redact_value(value: Any) -> Any:
    """Walk a JSON-like value and redact the env token plus secret-looking keys."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            name = str(key)
            redacted[name] = "[redacted]" if _SENSITIVE_KEY.search(name) else redact_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def public_error(exc: BaseException) -> str:
    text = redact(str(exc)).replace("\n", " ").strip()
    if not text:
        text = exc.__class__.__name__
    if len(text) > 300:
        text = text[:300] + "..."
    return text
