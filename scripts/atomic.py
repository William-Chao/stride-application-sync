"""Atomic JSON writes so a reader never observes a half-written file."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def dump_json(payload: object) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, payload: object) -> None:
    atomic_write_text(path, dump_json(payload))


def write_json_if_changed(path: Path, payload: dict) -> bool:
    """Write payload unless the existing file already matches, ignoring generated_at.

    Returns True when a new file is written. An unchanged file is left byte-identical,
    including its original generated_at.
    """
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if isinstance(current, dict) and _without_generated_at(current) == _without_generated_at(payload):
            return False
    atomic_write_json(path, payload)
    return True


def _without_generated_at(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key != "generated_at"}
