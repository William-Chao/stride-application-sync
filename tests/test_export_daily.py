"""Daily export layout, idempotency, schema, and PRs."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.export_daily import export
from scripts.sync_speediance import sync
from tests.helpers import validate_schema


def _run(tmp_path: Path, monkeypatch, now: str = "2026-10-14T20:30:00-07:00") -> Path:
    monkeypatch.setenv("SPEEDIANCE_FAKE", "1")
    monkeypatch.setenv("SPEEDIANCE_NOW", now)
    monkeypatch.setenv("SPEEDIANCE_START_DATE", "2026-01-01")
    monkeypatch.delenv("SPEEDIANCE_TOKEN", raising=False)
    monkeypatch.delenv("SPEEDIANCE_USER_ID", raising=False)
    data = tmp_path / "data"
    daily = tmp_path / "daily"
    assert sync(data) == 0
    assert export(data, daily) == 0
    return daily


def test_training_days_latest_and_prs(tmp_path, monkeypatch):
    daily = _run(tmp_path, monkeypatch)
    names = sorted(path.name for path in daily.iterdir())
    assert "2026-10-13.json" not in names
    assert names == [
        "2026-09-02.json",
        "2026-09-16.json",
        "2026-10-01.json",
        "2026-10-07.json",
        "2026-10-12.json",
        "latest.json",
    ]
    latest = json.loads((daily / "latest.json").read_text(encoding="utf-8"))
    validate_schema(latest, "latest.schema.json")
    assert latest["date"] == "2026-10-12"
    assert latest["file"] == "2026-10-12.json"

    day = json.loads((daily / "2026-10-12.json").read_text(encoding="utf-8"))
    validate_schema(day, "daily.schema.json")
    assert day["source"] == "speediance"
    assert day["version"] == "1.3.0"
    assert [session["start_time"] for session in day["sessions"]] == [
        "2026-10-12T18:05:00-07:00",
        "2026-10-12T19:40:00-07:00",
    ]
    assert day["sessions"][0]["calories_estimated"] is True
    assert day["sessions"][0]["duration_min"] == 41
    assert day["sessions"][0]["total_volume_kg"] == 432.0
    assert day["sessions"][0]["vendor_volume_kg"] == 0.0
    incline = day["sessions"][0]["exercises"][1]["sets"][0]
    assert "mode" not in incline
    assert day["sessions"][0]["exercises"][0]["sets"][0]["mode"] == 1
    plank = day["sessions"][1]["exercises"][1]
    assert plank["name"] == "Plank"
    assert plank["muscle_group"] == "core"
    assert plank["muscle_group_raw"] == "Core"
    assert plank["sets"] == [{"duration_s": 45, "weight_kg": None}]
    assert day["sessions"][1]["muscle_groups"] == ["chest", "core"]
    assert day["sessions"][1]["detail_available"] is True

    prs = {(item["exercise"], item["metric"]): item for item in day["prs"]}
    assert prs[("Bench Press", "est_1rm_kg")] == {
        "exercise": "Bench Press",
        "metric": "est_1rm_kg",
        "old": 86.67,
        "new": 88.0,
    }
    assert prs[("Bench Press", "top_set_kg")]["old"] == 70.0
    assert prs[("Bench Press", "top_set_kg")]["new"] == 80.0
    assert prs[("Incline DB Press", "est_1rm_kg")]["old"] is None
    assert prs[("Incline DB Press", "est_1rm_kg")]["new"] == 30.4
    assert prs[("Incline DB Press", "top_set_kg")]["old"] is None
    assert prs[("Incline DB Press", "top_set_kg")]["new"] == 24.0

    october_7 = json.loads((daily / "2026-10-07.json").read_text(encoding="utf-8"))
    metrics = {(item["exercise"], item["metric"]) for item in october_7["prs"]}
    assert ("Bench Press", "est_1rm_kg") in metrics
    assert ("Bench Press", "top_set_kg") not in metrics
    validate_schema(october_7, "daily.schema.json")

    rdl_day = json.loads((daily / "2026-10-01.json").read_text(encoding="utf-8"))
    assert rdl_day["sessions"][0]["exercises"][0]["sets"][0]["weight_kg"] == 61.23
    assert rdl_day["sessions"][0]["total_volume_kg"] == 918.45
    assert rdl_day["prs"][0]["old"] is None


def test_rerun_is_idempotent_and_rest_day_file_is_removed(tmp_path, monkeypatch):
    daily = _run(tmp_path, monkeypatch)
    before = {path.name: path.read_bytes() for path in daily.iterdir()}
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T21:15:00-07:00")
    assert export(tmp_path / "data", daily) == 0
    after = {path.name: path.read_bytes() for path in daily.iterdir()}
    assert after == before

    rest = daily / "2026-10-13.json"
    rest.write_text(json.dumps({"source": "speediance", "date": "2026-10-13"}) + "\n", encoding="utf-8")
    outsider = daily / "2026-11-01.json"
    outsider.write_text("{}\n", encoding="utf-8")
    notes = daily / "notes.json"
    notes.write_text("{}\n", encoding="utf-8")
    assert export(tmp_path / "data", daily) == 0
    assert not rest.exists()
    assert outsider.exists()
    assert notes.exists()
    assert (daily / "2026-10-12.json").read_bytes() == before["2026-10-12.json"]


def test_changed_day_is_rewritten_and_other_days_stay_put(tmp_path, monkeypatch):
    daily = _run(tmp_path, monkeypatch)
    before = {path.name: path.read_bytes() for path in daily.iterdir()}
    history_path = tmp_path / "data" / "speediance" / "normalized" / "history.json"
    history = json.loads(history_path.read_text(encoding="utf-8"))
    for session in history["sessions"]:
        if session["date"] == "2026-09-02":
            session["calories"] = 999
    history_path.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T22:00:00-07:00")
    assert export(tmp_path / "data", daily) == 0
    assert (daily / "2026-09-02.json").read_bytes() != before["2026-09-02.json"]
    assert (daily / "2026-10-12.json").read_bytes() == before["2026-10-12.json"]
    assert (daily / "latest.json").read_bytes() == before["latest.json"]
    changed = json.loads((daily / "2026-09-02.json").read_text(encoding="utf-8"))
    assert changed["generated_at"] == "2026-10-14T22:00:00-07:00"
    assert changed["sessions"][0]["calories"] == 999
    validate_schema(changed, "daily.schema.json")

