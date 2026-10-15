"""The fake pipeline runs sync then export and leaves a schema-valid tree."""

from __future__ import annotations

import json
import subprocess
import sys

from scripts.constants import ROOT
from tests.helpers import pipeline_env, validate_schema


def test_fake_pipeline_end_to_end(tmp_path):
    env = pipeline_env(tmp_path)
    proc = subprocess.run(
        [sys.executable, "scripts/run_pipeline.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = tmp_path / "data"
    daily = tmp_path / "daily"
    history = json.loads((data / "speediance" / "normalized" / "history.json").read_text(encoding="utf-8"))
    by_exercise = json.loads((data / "speediance" / "normalized" / "by_exercise.json").read_text(encoding="utf-8"))
    status = json.loads((data / "status.json").read_text(encoding="utf-8"))
    validate_schema(history, "history.schema.json")
    validate_schema(by_exercise, "by_exercise.schema.json")
    validate_schema(status, "status.schema.json")
    assert status["sources"]["speediance"]["status"] == "ok"
    assert status["sources"]["speediance"]["error"] is None
    assert (data / "speediance" / "raw" / "monthly" / "2026-09.json").is_file()
    assert (data / "speediance" / "raw" / "monthly" / "2026-10.json").is_file()
    raw = (data / "speediance" / "raw" / "monthly" / "2026-09.json").read_text(encoding="utf-8")
    assert "trainingId" in raw
    assert "Token" not in raw
    assert "App_user_id" not in raw

    day_names = sorted(path.name for path in daily.iterdir())
    assert "latest.json" in day_names
    assert "2026-10-13.json" not in day_names
    assert all(name == "latest.json" or name.endswith(".json") for name in day_names)
    latest = json.loads((daily / "latest.json").read_text(encoding="utf-8"))
    validate_schema(latest, "latest.schema.json")
    assert latest == {
        "schema_version": "1.3.0",
        "source": "speediance",
        "date": "2026-10-12",
        "file": "2026-10-12.json",
        "generated_at": "2026-10-14T20:30:00-07:00",
    }
    day = json.loads((daily / "2026-10-12.json").read_text(encoding="utf-8"))
    validate_schema(day, "daily.schema.json")
    prs = {(item["exercise"], item["metric"]): item for item in day["prs"]}
    assert prs[("Bench Press", "est_1rm_kg")]["old"] == 86.67
    assert prs[("Bench Press", "est_1rm_kg")]["new"] == 88.0
    assert prs[("Bench Press", "top_set_kg")]["old"] == 70.0
    assert prs[("Bench Press", "top_set_kg")]["new"] == 80.0
    assert prs[("Incline DB Press", "top_set_kg")]["old"] is None

    before = {path.name: path.read_bytes() for path in daily.iterdir()}
    env["SPEEDIANCE_NOW"] = "2026-10-14T23:05:00-07:00"
    again = subprocess.run(
        [sys.executable, "scripts/run_pipeline.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert again.returncode == 0, again.stderr + again.stdout
    after = {path.name: path.read_bytes() for path in daily.iterdir()}
    assert after == before
    assert list(data.rglob("*.tmp")) == []
    assert list(daily.rglob("*.tmp")) == []
