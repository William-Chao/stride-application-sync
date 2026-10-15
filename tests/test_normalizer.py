"""Normalizer: units, Epley, both volume units, and schema."""

from __future__ import annotations

import json
from datetime import date

from scripts.constants import ROOT
from scripts.export_daily import build_day_file
from scripts.normalize_speediance import epley_kg, kg_to_lb, lb_to_kg, normalize_bundles
from scripts.speediance_client import FakeSpeedianceClient
from tests.helpers import validate_schema

STAMP = "2026-10-14T20:30:00-07:00"


def _history():
    client = FakeSpeedianceClient(ROOT / "tests" / "fixtures" / "speediance_payload.json")
    bundles = client.fetch_between(date(2026, 1, 1), date(2026, 10, 14))
    history, by_exercise = normalize_bundles(
        bundles,
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    return history, by_exercise


def test_epley_and_unit_conversion():
    assert epley_kg(60, 8) == 76.0
    assert epley_kg(70, 5) == 81.67
    assert epley_kg(65, 10) == 86.67
    assert epley_kg(80, 3) == 88.0
    assert epley_kg(24, 8) == 30.4
    assert kg_to_lb(60) == 132.28
    assert lb_to_kg(135) == 61.23
    assert epley_kg(lb_to_kg(135), 5) == 71.44
    assert kg_to_lb(1440) == 3174.66


def test_normalized_sessions_have_explicit_units_and_match_schema():
    history, by_exercise = _history()
    validate_schema(history, "history.schema.json")
    validate_schema(by_exercise, "by_exercise.schema.json")

    first = history["sessions"][0]
    assert first["training_id"] == "1001"
    assert first["date"] == "2026-09-02"
    assert first["start_time"] == "2026-09-02T18:00:00-07:00"
    assert first["title"] == "Upper A"
    assert first["duration_seconds"] == 3000
    assert first["calories"] == 250.0
    assert first["template_name"] == "Upper A"
    assert first["exercise_count"] == 2
    assert first["total_volume"] == {"kg": 2040.0, "lb": 4497.43}
    assert first["vendor_volume_kg"] == 0.0
    bench = first["exercise_breakdown"][0]
    assert bench["name"] == "Bench Press"
    assert bench["muscle_group"] == "chest"
    assert len(bench["sets"]) == 3
    assert bench["sets"][0]["weight_kg"] == 60.0
    assert bench["sets"][0]["weight_lb"] == 132.28
    assert bench["sets"][0]["estimated_1rm_kg"] == 76.0
    assert bench["sets"][0]["side"] is None
    assert bench["sets"][0]["mode"] == 1

    row = first["exercise_breakdown"][1]
    assert [item["side"] for item in row["sets"]] == ["left", "right"]

    finisher = next(session for session in history["sessions"] if session["training_id"] == "1006")
    assert finisher["template_name"] is None
    plank = finisher["exercise_breakdown"][1]
    assert plank["name"] == "Plank"
    assert plank["muscle_group"] == "core"
    assert plank["muscle_group_raw"] == "Core"
    assert plank["sets"][0]["reps"] is None
    assert plank["sets"][0]["weight_kg"] is None
    assert plank["sets"][0]["weight_lb"] is None
    assert plank["sets"][0]["duration_s"] == 45
    assert plank["sets"][0]["estimated_1rm_kg"] is None
    assert "mode" not in plank["sets"][0]
    assert finisher["detail_available"] is True
    assert finisher["muscle_groups"] == ["chest", "core"]

    names = [movement["name"] for movement in by_exercise["movements"]]
    assert "Plank" not in names
    row_sets = next(movement for movement in by_exercise["movements"] if movement["name"] == "Dumbbell Row")
    assert [item["side"] for item in row_sets["sets"]] == ["left", "right"]
    assert row_sets["sets"][0]["session_id"] == "1001"
    assert row_sets["sets"][0]["timestamp"] == "2026-09-02T18:00:00-07:00"
    assert row_sets["sets"][0]["weight"] == {"kg": 30.0, "lb": 66.14}
    assert row_sets["sets"][0]["estimated_1rm"]["kg"] == 40.0

    rdl = next(session for session in history["sessions"] if session["training_id"] == "1003")
    rdl_set = rdl["exercise_breakdown"][0]["sets"][0]
    assert rdl_set["weight_lb"] == 135.0
    assert rdl_set["weight_kg"] == 61.23
    assert rdl_set["estimated_1rm_kg"] == 71.44
    assert rdl["total_volume"]["kg"] == 918.45
    assert rdl["vendor_volume_kg"] == 0.0
    assert rdl["exercise_breakdown"][0]["muscle_group"] == "legs"
    assert rdl["exercise_breakdown"][0]["muscle_group_raw"] == "Hamstrings"


def test_millisecond_timestamps_convert_to_los_angeles():
    bundle = {
        "source_weight_unit": "kg",
        "summary": {
            "trainingId": 42,
            "title": "Ms",
            "trainingTime": 60,
            "calorie": 10,
            "startTimestamp": 1788397200000,
        },
        "session": {},
        "detail": [],
    }
    history, _ = normalize_bundles(
        [bundle],
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    assert history["sessions"][0]["start_time"] == "2026-09-02T18:00:00-07:00"
    assert history["sessions"][0]["date"] == "2026-09-02"
    assert history["sessions"][0]["vendor_volume_kg"] is None
    assert history["sessions"][0]["total_volume"]["kg"] == 0.0


def _raise_bundle(*, training_id: int, capacity, unit: str, session_capacity=None) -> dict:
    summary = {
        "trainingId": training_id,
        "title": "Strength",
        "trainingTime": 60,
        "calorie": 1,
        "startTimestamp": 1791853500,
    }
    if capacity is not None:
        summary["totalCapacity"] = capacity
    session = {}
    if session_capacity is not None:
        session["totalCapacity"] = session_capacity
    return {
        "source_weight_unit": unit,
        "summary": summary,
        "session": session,
        "detail_available": True,
        "detail": [
            {
                "actionLibraryName": "Standing I Raise",
                "mainMuscleGroupName": "Front Delts",
                "finishedReps": [
                    {
                        "finishedCount": 10,
                        "leftRight": 0,
                        "mode": 1,
                        "trainingInfoDetail": {"weights": [8, 8, 8, 8, 8, 8, 8, 8, 8, 8]},
                    }
                ],
            }
        ],
    }


def test_vendor_volume_kg_keeps_the_vendor_load_apart_from_set_volume():
    history, _ = normalize_bundles(
        [
            _raise_bundle(training_id=670355, capacity=8028, unit="kg"),
            _raise_bundle(training_id=670356, capacity=135, unit="lb", session_capacity=999),
            _raise_bundle(training_id=670357, capacity=None, unit="kg", session_capacity=40),
        ],
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    validate_schema(history, "history.schema.json")
    by_id = {session["training_id"]: session for session in history["sessions"]}

    metric = by_id["670355"]
    assert metric["total_volume"]["kg"] == 80.0
    assert metric["vendor_volume_kg"] == 8028.0

    imperial = by_id["670356"]
    assert imperial["total_volume"]["kg"] == 36.3
    assert imperial["vendor_volume_kg"] == 61.23

    from_session = by_id["670357"]
    assert from_session["total_volume"]["kg"] == 80.0
    assert from_session["vendor_volume_kg"] == 40.0

    day, _prior = build_day_file(metric["date"], [metric], {}, STAMP)
    validate_schema(day, "daily.schema.json")
    assert day["sessions"][0]["total_volume_kg"] == 80.0
    assert day["sessions"][0]["vendor_volume_kg"] == 8028.0


def test_normalized_files_do_not_embed_fixture_as_raw_only_fields():
    history, _ = _history()
    encoded = json.dumps(history)
    assert "trainingInfoDetail" not in encoded
    assert "App_user_id" not in encoded
