"""Load a local .env without printing values or overriding the real environment."""

from __future__ import annotations

import os
from pathlib import Path

from scripts.constants import DEFAULT_DAILY_DIR, DEFAULT_OUTPUT_DIR, ROOT


def load_dotenv(path: Path | None = None) -> None:
    env_path = path or (ROOT / ".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def output_dir() -> Path:
    raw = os.environ.get("SPEEDIANCE_OUTPUT_DIR", "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_OUTPUT_DIR
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def daily_dir() -> Path:
    raw = os.environ.get("SPEEDIANCE_DAILY_DIR", "").strip()
    path = Path(raw).expanduser() if raw else DEFAULT_DAILY_DIR
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()

