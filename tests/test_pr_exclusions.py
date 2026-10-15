"""Committed PR exclusions and the matcher that applies them."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.export_daily import _day_bests, build_day_file
from scripts.normalize_speediance import epley_kg, lb_to_kg, normalize_bundles
from scripts.pr_exclusions import PrExclusionError, load_exclusions, matches, parse_exclusions
from tests.helpers import validate_schema

STAMP = "2026-10-14T20:30:00-07:00"


def _bundle(training_id: int, start: str, exercises: list[dict], unit: str = "kg") -> dict:
    return {
        "source_weight_unit": unit,
        "detail_available": True,
        "summary": {
            "trainingId": training_id,
            "title": "Lift",
            "trainingTime": 3600,
            "calorie": 100,
            "startTime": start,
        },
        "session": {},
        "detail": exercises,
    }


def _exercise(name: str, sets: list[dict], muscle: str = "Back") -> dict:
    return {
        "actionLibraryName": name,
        "trainingPartName": muscle,
        "finishedReps": sets,
    }


def _normalize(bundles: list[dict], exclusions: list[dict] | None = None) -> tuple[dict, dict]:
    kwargs = {"generated_at": STAMP, "source_status": "ok", "last_successful_sync": STAMP}
    if exclusions is not None:
        kwargs["exclusions"] = exclusions
    return normalize_bundles(bundles, **kwargs)


def _days(history: dict) -> dict[str, dict]:
    by_day: dict[str, list[dict]] = {}
    for session in history["sessions"]:
        by_day.setdefault(session["date"], []).append(session)
    prior: dict[str, dict[str, float]] = {}
    files = {}
    for day in sorted(by_day):
        sessions = sorted(by_day[day], key=lambda item: (item["start_time"], item["training_id"]))
        payload, prior = build_day_file(day, sessions, prior, STAMP)
        validate_schema(payload, "daily.schema.json")
        files[day] = payload
    return files


def test_committed_file_is_empty_by_default():
    assert load_exclusions() == []


def test_matcher_respects_date_name_and_minimum_weight():
    rule = {"date": "2025-11-01", "exercise": "Barbell Deadlift", "min_weight_kg": 220}
    assert matches(rule, "2025-11-01", "Barbell Deadlift", 220)
    assert matches(rule, "2025-11-01", " Barbell Deadlift ", 220.0)
    assert not matches(rule, "2025-11-01", "Barbell Deadlift", 219.99)
    assert not matches(rule, "2025-11-01", "Barbell Deadlift", None)
    assert not matches(rule, "2025-11-01", "barbell deadlift", 220)
    assert not matches(rule, "2025-11-02", "Barbell Deadlift", 250)
    assert not matches(rule, "2025-11-01", "Bench Press", 220)

    open_rule = {"date": "2025-11-01", "exercise": "Plank"}
    assert matches(open_rule, "2025-11-01", "Plank", None)
    assert matches(open_rule, "2025-11-01", "Plank", 20)
    assert not matches(open_rule, "2025-10-01", "Plank", None)


def test_broken_exclusion_file_raises(tmp_path: Path):
    with pytest.raises(PrExclusionError):
        parse_exclusions([])
    with pytest.raises(PrExclusionError):
        parse_exclusions({"exclusions": [{"date": "2025-11-01"}]})
    with pytest.raises(PrExclusionError):
        parse_exclusions({"exclusions": [{"date": "2025-13-01", "exercise": "Barbell Deadlift"}]})
    with pytest.raises(PrExclusionError):
        parse_exclusions(
            {"exclusions": [{"date": "2025-11-01", "exercise": "Barbell Deadlift", "min_weight_kg": "heavy"}]}
        )
    with pytest.raises(PrExclusionError):
        parse_exclusions(
            {"exclusions": [{"date": "2025-11-01", "exercise": "Barbell Deadlift", "minimum": 220}]}
        )
    assert load_exclusions(tmp_path / "missing.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(PrExclusionError):
        load_exclusions(bad)


HEAVY_DEADLIFT_RULE = [{"date": "2025-11-01", "exercise": "Barbell Deadlift", "min_weight_kg": 220}]


def test_heavy_deadlift_is_left_out_of_prs_e1rm_and_by_exercise():
    history, by_exercise = _normalize(
        [
            _bundle(
                501,
                "2025-10-01T18:00:00-07:00",
                [_exercise("Barbell Deadlift", [{"finishedCount": 5, "weight": 100}])],
            ),
            _bundle(
                502,
                "2025-11-01T18:00:00-07:00",
                [
                    _exercise(
                        "Barbell Deadlift",
                        [
                            {"finishedCount": 3, "weight": 140},
                            {"finishedCount": 1, "weight": 220},
                        ],
                    ),
                    _exercise("Bench Press", [{"finishedCount": 1, "weight": 220}], "Chest"),
                ],
            ),
            _bundle(
                503,
                "2025-11-01T19:00:00-07:00",
                [_exercise("Barbell Deadlift", [{"finishedCount": 1, "weight": 220}])],
                unit="lb",
            ),
            _bundle(
                504,
                "2025-11-02T18:00:00-08:00",
                [_exercise("Barbell Deadlift", [{"finishedCount": 1, "weight": 220}])],
            ),
        ],
        HEAVY_DEADLIFT_RULE,
    )
    validate_schema(history, "history.schema.json")
    validate_schema(by_exercise, "by_exercise.schema.json")

    november = next(session for session in history["sessions"] if session["training_id"] == "502")
    deadlift = next(item for item in november["exercise_breakdown"] if item["name"] == "Barbell Deadlift")
    warmup, heavy = deadlift["sets"]
    assert warmup["weight_kg"] == 140.0
    assert warmup["reps"] == 3
    assert warmup["estimated_1rm_kg"] == 154.0
    assert "excluded_from_prs" not in warmup
    assert heavy["weight_kg"] == 220.0
    assert heavy["reps"] == 1
    assert heavy["excluded_from_prs"] is True
    assert heavy["estimated_1rm_kg"] is None
    assert heavy["estimated_1rm_lb"] is None
    assert november["total_volume"]["kg"] == 860.0

    bench = next(item for item in november["exercise_breakdown"] if item["name"] == "Bench Press")
    assert "excluded_from_prs" not in bench["sets"][0]
    assert bench["sets"][0]["estimated_1rm_kg"] == 227.33

    pounds = next(session for session in history["sessions"] if session["training_id"] == "503")
    pound_set = pounds["exercise_breakdown"][0]["sets"][0]
    assert pound_set["weight_kg"] == lb_to_kg(220)
    assert pound_set["weight_kg"] < 220
    assert "excluded_from_prs" not in pound_set
    assert pound_set["estimated_1rm_kg"] == epley_kg(lb_to_kg(220), 1)

    later = next(session for session in history["sessions"] if session["training_id"] == "504")
    later_set = later["exercise_breakdown"][0]["sets"][0]
    assert later["date"] == "2025-11-02"
    assert "excluded_from_prs" not in later_set
    assert later_set["estimated_1rm_kg"] == 227.33

    movement = next(item for item in by_exercise["movements"] if item["name"] == "Barbell Deadlift")
    assert [(item["weight"]["kg"], item["timestamp"][:10]) for item in movement["sets"]] == [
        (100.0, "2025-10-01"),
        (140.0, "2025-11-01"),
        (lb_to_kg(220), "2025-11-01"),
        (220.0, "2025-11-02"),
    ]
    bench_movement = next(item for item in by_exercise["movements"] if item["name"] == "Bench Press")
    assert bench_movement["sets"][0]["weight"]["kg"] == 220.0
    assert bench_movement["sets"][0]["estimated_1rm"]["kg"] == 227.33

    files = _days(history)
    day = files["2025-11-01"]
    exported = next(session for session in day["sessions"] if session["start_time"].startswith("2025-11-01T18:00:00"))
    assert exported["total_volume_kg"] == 860.0
    exported_deadlift = next(item for item in exported["exercises"] if item["name"] == "Barbell Deadlift")
    assert exported_deadlift["sets"] == [
        {"weight_kg": 140.0, "reps": 3},
        {"weight_kg": 220.0, "reps": 1, "excluded_from_prs": True},
    ]
    prs = {(item["exercise"], item["metric"]): item for item in day["prs"]}
    assert prs[("Barbell Deadlift", "est_1rm_kg")] == {
        "exercise": "Barbell Deadlift",
        "metric": "est_1rm_kg",
        "old": 116.67,
        "new": 154.0,
    }
    assert prs[("Barbell Deadlift", "top_set_kg")]["old"] == 100.0
    assert prs[("Barbell Deadlift", "top_set_kg")]["new"] == 140.0
    assert prs[("Bench Press", "top_set_kg")]["old"] is None
    assert prs[("Bench Press", "top_set_kg")]["new"] == 220.0
    assert prs[("Bench Press", "est_1rm_kg")]["new"] == 227.33

    following = {(item["exercise"], item["metric"]): item for item in files["2025-11-02"]["prs"]}
    assert following[("Barbell Deadlift", "top_set_kg")]["old"] == 140.0
    assert following[("Barbell Deadlift", "top_set_kg")]["new"] == 220.0
    assert following[("Barbell Deadlift", "est_1rm_kg")]["old"] == 154.0
    assert following[("Barbell Deadlift", "est_1rm_kg")]["new"] == 227.33


def test_lighter_same_day_deadlift_still_counts():
    history, by_exercise = _normalize(
        [
            _bundle(
                1,
                "2025-11-01T18:00:00-07:00",
                [_exercise("Barbell Deadlift", [{"finishedCount": 1, "weight": 219.99}])],
            )
        ]
    )
    item = history["sessions"][0]["exercise_breakdown"][0]["sets"][0]
    assert item["weight_kg"] == 219.99
    assert "excluded_from_prs" not in item
    assert item["estimated_1rm_kg"] == epley_kg(219.99, 1)
    assert by_exercise["movements"][0]["sets"][0]["weight"]["kg"] == 219.99


def test_same_set_counts_when_no_rules_are_loaded():
    history, by_exercise = _normalize(
        [
            _bundle(
                1,
                "2025-11-01T18:00:00-07:00",
                [_exercise("Barbell Deadlift", [{"finishedCount": 1, "weight": 220}])],
            )
        ],
        exclusions=[],
    )
    item = history["sessions"][0]["exercise_breakdown"][0]["sets"][0]
    assert "excluded_from_prs" not in item
    assert item["estimated_1rm_kg"] == 227.33
    assert by_exercise["movements"][0]["sets"][0]["estimated_1rm"]["kg"] == 227.33


def test_rule_without_min_weight_excludes_every_set_that_day():
    rules = [{"date": "2025-11-01", "exercise": "Farmer Carry"}]
    history, by_exercise = _normalize(
        [
            _bundle(
                1,
                "2025-11-01T18:00:00-07:00",
                [
                    _exercise(
                        "Farmer Carry",
                        [
                            {"finishedCount": 5, "weight": 40},
                            {"finishedCount": 0, "time": 30},
                        ],
                    ),
                    _exercise("Row", [{"finishedCount": 8, "weight": 50}]),
                ],
            ),
            _bundle(
                2,
                "2025-11-02T18:00:00-08:00",
                [_exercise("Farmer Carry", [{"finishedCount": 5, "weight": 40}])],
            ),
        ],
        exclusions=rules,
    )
    validate_schema(history, "history.schema.json")
    validate_schema(by_exercise, "by_exercise.schema.json")
    first = history["sessions"][0]
    carry, row = first["exercise_breakdown"]
    loaded, timed = carry["sets"]
    assert loaded["excluded_from_prs"] is True
    assert loaded["estimated_1rm_kg"] is None
    assert loaded["weight_kg"] == 40.0
    assert timed["excluded_from_prs"] is True
    assert timed["weight_kg"] is None
    assert timed["duration_s"] == 30
    assert "excluded_from_prs" not in row["sets"][0]
    assert first["total_volume"]["kg"] == 600.0
    names = {item["name"]: [set_["weight"]["kg"] for set_ in item["sets"]] for item in by_exercise["movements"]}
    assert names["Farmer Carry"] == [40.0]
    assert names["Row"] == [50.0]
    day = _days(history)["2025-11-01"]
    exported = next(item for item in day["sessions"][0]["exercises"] if item["name"] == "Farmer Carry")
    assert exported["sets"][0]["excluded_from_prs"] is True
    assert exported["sets"][1] == {"duration_s": 30, "weight_kg": None, "excluded_from_prs": True}
    assert all(item["exercise"] != "Farmer Carry" for item in day["prs"])


def test_day_bests_skip_a_flagged_set_that_still_has_an_estimate():
    best = _day_bests(
        [
            {
                "exercise_breakdown": [
                    {
                        "name": "Barbell Deadlift",
                        "sets": [
                            {
                                "weight_kg": 220.0,
                                "reps": 1,
                                "estimated_1rm_kg": 227.33,
                                "excluded_from_prs": True,
                            },
                            {"weight_kg": 140.0, "reps": 3, "estimated_1rm_kg": 154.0},
                        ],
                    }
                ]
            }
        ]
    )
    assert best == {"Barbell Deadlift": {"est_1rm_kg": 154.0, "top_set_kg": 140.0}}
