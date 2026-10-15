"""Shared schema checks for the ingestion tests."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def validate_schema(instance: object, schema_name: str) -> None:
    schema = json.loads((ROOT / "schema" / schema_name).read_text(encoding="utf-8"))
    jsonschema.validate(instance, schema)


def pipeline_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    import os

    env = os.environ.copy()
    env.pop("SPEEDIANCE_TOKEN", None)
    env.pop("SPEEDIANCE_USER_ID", None)
    env.update(
        {
            "SPEEDIANCE_FAKE": "1",
            "SPEEDIANCE_OUTPUT_DIR": str(tmp_path / "data"),
            "SPEEDIANCE_DAILY_DIR": str(tmp_path / "daily"),
            "SPEEDIANCE_NOW": "2026-10-14T20:30:00-07:00",
            "SPEEDIANCE_START_DATE": "2026-01-01",
        }
    )
    env.update(extra)
    return env
