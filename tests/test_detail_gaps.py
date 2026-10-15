"""Detail namespaces, muscle mapping, unloaded sets, and the profile weight unit."""

from __future__ import annotations

import json
import logging
from datetime import date

from scripts.constants import ROOT
from scripts.normalize_speediance import map_muscle, normalize_bundles, other_muscle_raws
from scripts.speediance_client import FakeSpeedianceClient, SpeedianceClient
from scripts.sync_speediance import sync
from tests.helpers import validate_schema

STAMP = "2026-10-14T20:30:00-07:00"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Response:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class ScriptedSession:
    def __init__(self, responses: list[Response]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"unexpected request {url}")
        return self.responses.pop(0)


def _ok(data) -> Response:
    return Response({"code": 0, "data": data})


def _client(session: ScriptedSession) -> SpeedianceClient:
    return SpeedianceClient(
        "user-1",
        "token-value-not-logged",
        session=session,
        sleeper=Clock().sleep,
        monotonic=Clock().monotonic,
        request_delay=0,
    )


def _record(training_id: int, training_type: int) -> dict:
    return {
        "trainingId": training_id,
        "type": training_type,
        "title": "Session",
        "trainingTime": 60,
        "calorie": 1,
        "startTimestamp": 1791853500,
    }


def _plan_raise() -> dict:
    return {
        "actionLibraryName": "Standing I Raise",
        "sets": [
            {
                "targetCount": 12,
                "targetWeight": 20,
                "finishedCount": 10,
                "finishedWeight": 8,
                "leftRight": 0,
                "mode": 1,
            },
            {
                "targetCount": 12,
                "targetWeight": 20,
                "finishedCount": 0,
                "trainingInfoDetail": {"weights": [20, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20, 20]},
            },
        ],
    }


def _bench(group_id: int | None = None, muscle: str | None = None) -> dict:
    exercise = {
        "actionLibraryName": "Bench Press",
        "finishedReps": [
            {
                "finishedCount": 5,
                "leftRight": 0,
                "mode": 1,
                "trainingInfoDetail": {"weights": [20, 20, 20, 20, 20]},
            }
        ],
    }
    if group_id is not None:
        exercise["actionLibraryGroupId"] = group_id
    if muscle is not None:
        exercise["mainMuscleGroupName"] = muscle
    return exercise


def test_muscle_names_map_onto_the_fixed_set():
    assert map_muscle("Pecs") == "chest"
    assert map_muscle("Chest") == "chest"
    assert map_muscle("Lats") == "back"
    assert map_muscle("Traps") == "back"
    assert map_muscle("Front Delts") == "shoulders"
    assert map_muscle("Biceps") == "biceps"
    assert map_muscle("Triceps") == "triceps"
    assert map_muscle("Hamstrings") == "legs"
    assert map_muscle("Quads") == "legs"
    assert map_muscle("Glutes") == "glutes"
    assert map_muscle("Abs") == "core"
    assert map_muscle("Full Body") == "full_body"
    assert map_muscle("Forearms") == "other"
    assert map_muscle(None) == "other"
    assert map_muscle("胸") == "chest"


def test_gap_fixture_covers_unloaded_sets_and_summary_only_sessions():
    client = FakeSpeedianceClient(ROOT / "tests" / "fixtures" / "speediance_gaps.json")
    history, by_exercise = normalize_bundles(
        client.fetch_between(date(2026, 1, 1), date(2026, 12, 31)),
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    validate_schema(history, "history.schema.json")
    validate_schema(by_exercise, "by_exercise.schema.json")

    free = next(session for session in history["sessions"] if session["training_id"] == "2001")
    assert free["detail_available"] is True
    assert free["total_volume"] == {"kg": 0.0, "lb": 0.0}
    assert free["muscle_groups"] == ["chest", "shoulders", "glutes"]
    push = free["exercise_breakdown"][0]
    assert push["muscle_group"] == "chest"
    assert push["muscle_group_raw"] == "Pecs"
    assert push["sets"] == [
        {
            "reps": 12,
            "side": None,
            "weight_kg": None,
            "weight_lb": None,
            "estimated_1rm_kg": None,
            "estimated_1rm_lb": None,
        }
    ]
    raise_set = free["exercise_breakdown"][1]["sets"][0]
    assert raise_set["reps"] == 10
    assert raise_set["duration_s"] == 20
    assert raise_set["weight_kg"] is None
    assert free["exercise_breakdown"][1]["muscle_group"] == "shoulders"
    assert free["exercise_breakdown"][1]["muscle_group_raw"] == "Front Delts"

    quick = next(session for session in history["sessions"] if session["training_id"] == "2002")
    assert quick["detail_available"] is True
    assert quick["muscle_groups"] == ["core", "full_body", "other"]
    dead_bug = quick["exercise_breakdown"][0]["sets"][0]
    assert dead_bug["reps"] is None
    assert dead_bug["duration_s"] == 30
    assert dead_bug["weight_kg"] is None
    assert quick["exercise_breakdown"][0]["muscle_group_raw"] == "Abs"
    carry = quick["exercise_breakdown"][1]["sets"][0]
    assert carry["reps"] == 1
    assert carry["duration_s"] == 40
    assert carry["weight_kg"] is None
    assert quick["exercise_breakdown"][2]["muscle_group"] == "other"
    assert quick["exercise_breakdown"][2]["muscle_group_raw"] == "Forearms"

    summary = next(session for session in history["sessions"] if session["training_id"] == "2003")
    assert summary["detail_available"] is False
    assert summary["exercise_breakdown"] == []
    assert summary["muscle_groups"] == []
    assert summary["exercise_count"] == 0

    names = [movement["name"] for movement in by_exercise["movements"]]
    assert "Push-up" not in names
    assert "Dead Bug" not in names
    assert "Wrist Curl" not in names


def test_free_lift_and_quick_fall_through_to_free_training_detail():
    session = ScriptedSession(
        [
            _ok([_record(11, 1), _record(12, 7)]),
            Response({"code": 1002, "message": "not found"}),
            _ok([_bench(muscle="Pecs")]),
            _ok({"title": "Free Lift"}),
            Response({"code": 1002, "message": "not found"}),
            _ok([_bench(muscle="Abs")]),
            _ok({"name": "Quick"}),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert [bundle["detail_available"] for bundle in bundles] == [True, True]
    assert bundles[0]["detail"][0]["mainMuscleGroupName"] == "Pecs"
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == [
        "/api/app/trainingInfo/planTrainingInfoDetail/11",
        "/api/app/trainingInfo/freeTrainingDetail/11",
        "/api/app/trainingInfo/freeTraining/11",
        "/api/app/trainingInfo/planTrainingInfoDetail/12",
        "/api/app/trainingInfo/freeTrainingDetail/12",
        "/api/app/trainingInfo/freeTraining/12",
    ]


def test_program_and_custom_keep_their_detail_namespaces():
    session = ScriptedSession(
        [
            _ok([_record(21, 2), _record(22, 5)]),
            Response({"code": 1002, "message": "not found"}),
            _ok([_bench(muscle="Chest")]),
            _ok({"title": "Program"}),
            _ok([_bench(muscle="Back")]),
            _ok({"title": "Custom"}),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert all(bundle["detail_available"] for bundle in bundles)
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == [
        "/api/app/trainingInfo/planTrainingInfoDetail/21",
        "/api/app/trainingInfo/courseTrainingInfoDetail/21",
        "/api/app/trainingInfo/courseTrainingInfo/21",
        "/api/app/trainingInfo/cttTrainingInfoDetail/22",
        "/api/app/trainingInfo/cttTrainingInfo/22",
    ]


def test_empty_free_detail_keeps_searching_then_stays_summary_only():
    session = ScriptedSession(
        [
            _ok([_record(31, 1)]),
            _ok([]),
            _ok([]),
            _ok([]),
            _ok([]),
            _ok({"totalCapacity": 1200, "trainingTime": 600}),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is False
    assert bundles[0]["detail"] == []
    assert bundles[0]["session"]["totalCapacity"] == 1200
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == [
        "/api/app/trainingInfo/planTrainingInfoDetail/31",
        "/api/app/trainingInfo/freeTrainingDetail/31",
        "/api/app/trainingInfo/courseTrainingInfoDetail/31",
        "/api/app/trainingInfo/cttTrainingInfoDetail/31",
        "/api/app/trainingInfo/freeTraining/31",
    ]
    assert not any("/api/app/v2/" in call or "/api/app/v3/" in call for call in session.calls)


def test_empty_plan_uses_later_course_rows():
    session = ScriptedSession(
        [
            _ok([_record(32, 1)]),
            _ok([]),
            _ok([]),
            _ok([_bench(muscle="Pecs")]),
            _ok({"title": "Course"}),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    assert bundles[0]["detail"][0]["actionLibraryName"] == "Bench Press"
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == [
        "/api/app/trainingInfo/planTrainingInfoDetail/32",
        "/api/app/trainingInfo/freeTrainingDetail/32",
        "/api/app/trainingInfo/courseTrainingInfoDetail/32",
        "/api/app/trainingInfo/courseTrainingInfo/32",
    ]


def test_plan_rows_stop_the_search_and_skip_v2():
    row = _plan_raise()
    row["mainMuscleGroupName"] = "Front Delts"
    session = ScriptedSession(
        [
            _ok([_record(33, 1)]),
            _ok([row]),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == ["/api/app/trainingInfo/planTrainingInfoDetail/33"]
    assert not any("/api/app/v2/" in call or "/api/app/v3/" in call or "api3.speediance.com" in call for call in session.calls)


def test_alternate_record_id_is_used_when_training_id_has_no_rows(caplog):
    record = _record(61, 1)
    record["id"] = 62
    session = ScriptedSession(
        [
            _ok([record]),
            *([Response({"code": 1002, "message": "missing"})] * 4),
            _ok([_bench(muscle="Pecs")]),
        ]
    )
    client = _client(session)
    with caplog.at_level(logging.INFO, logger="speediance.client"):
        bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    assert bundles[0]["detail"][0]["mainMuscleGroupName"] == "Pecs"
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == [
        "/api/app/trainingInfo/planTrainingInfoDetail/61",
        "/api/app/trainingInfo/freeTrainingDetail/61",
        "/api/app/trainingInfo/courseTrainingInfoDetail/61",
        "/api/app/trainingInfo/cttTrainingInfoDetail/61",
        "/api/app/trainingInfo/planTrainingInfoDetail/62",
    ]
    assert "alternate id 62" in caplog.text


def test_rows_on_training_id_skip_the_other_record_id():
    record = _record(71, 1)
    record["id"] = 72
    session = ScriptedSession(
        [
            _ok([record]),
            _ok([_bench(muscle="Pecs")]),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    assert all(not call.endswith("/72") for call in session.calls)


def test_detail_miss_log_names_the_path(caplog):
    session = ScriptedSession(
        [
            _ok([_record(41, 8)]),
            Response({"code": 1002, "message": "not found"}),
            Response({"code": 1002, "message": "not found"}),
            _ok([_bench(muscle="Pecs")]),
            _ok({"title": "Course"}),
        ]
    )
    client = _client(session)
    with caplog.at_level(logging.INFO, logger="speediance.client"):
        bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    misses = [record.message for record in caplog.records if "had no payload" in record.message]
    assert misses == [
        "Speediance detail namespace had no payload path=/api/app/trainingInfo/planTrainingInfoDetail/41 message=not found",
        "Speediance detail namespace had no payload path=/api/app/trainingInfo/freeTrainingDetail/41 message=not found",
    ]


def test_missing_free_namespace_falls_through_to_course_then_custom():
    session = ScriptedSession(
        [
            _ok([_record(41, 8)]),
            Response({"code": 1002, "message": "not found"}),
            Response({"code": 1002, "message": "not found"}),
            _ok([_bench(muscle="Pecs")]),
            _ok({"title": "Course"}),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths[1:] == [
        "/api/app/trainingInfo/planTrainingInfoDetail/41",
        "/api/app/trainingInfo/freeTrainingDetail/41",
        "/api/app/trainingInfo/courseTrainingInfoDetail/41",
        "/api/app/trainingInfo/courseTrainingInfo/41",
    ]


def test_action_library_fills_a_missing_muscle_name():
    session = ScriptedSession(
        [
            _ok([_record(51, 1)]),
            _ok([_bench(group_id=434)]),
            _ok([{"id": 434, "mainMuscleGroupName": "Pecs", "title": "Bench Press"}]),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert "planTrainingInfoDetail/51" in session.calls[1]
    assert "actionLibraryGroup/list?ids=434" in session.calls[2]
    assert bundles[0]["detail"][0]["mainMuscleGroupName"] == "Pecs"
    history, _ = normalize_bundles(
        bundles,
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    exercise = history["sessions"][0]["exercise_breakdown"][0]
    assert exercise["muscle_group"] == "chest"
    assert exercise["muscle_group_raw"] == "Pecs"
    assert history["sessions"][0]["muscle_groups"] == ["chest"]
    assert history["sessions"][0]["detail_available"] is True
    validate_schema(history, "history.schema.json")


def test_plan_detail_uses_finished_reps_and_muscle_load_totals():
    session = ScriptedSession(
        [
            _ok([_record(670355, 1)]),
            _ok([_plan_raise()]),
            _ok(
                {
                    "muscleDetailList": [
                        {"muscleGroupName": "Front Delts", "capacity": 80},
                        {"muscleGroupName": "Chest", "capacity": 40},
                    ]
                }
            ),
        ]
    )
    client = _client(session)
    bundles = client.fetch_between(date(2026, 10, 1), date(2026, 10, 14))
    assert bundles[0]["detail_available"] is True
    assert bundles[0]["muscle_load_names"] == ["Front Delts", "Chest"]
    assert session.calls[-1].endswith("/api/mobile/muscleLoad/detail?trainingId=670355")
    history, _by_exercise = normalize_bundles(
        bundles,
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    validate_schema(history, "history.schema.json")
    exercise = history["sessions"][0]["exercise_breakdown"][0]
    assert exercise["name"] == "Standing I Raise"
    assert exercise["muscle_group_raw"] is None
    assert len(exercise["sets"]) == 1
    performed = exercise["sets"][0]
    assert performed["reps"] == 10
    assert performed["weight_kg"] == 8.0
    assert performed["mode"] == 1
    assert history["sessions"][0]["muscle_groups"] == ["shoulders", "chest"]
    assert history["sessions"][0]["total_volume"]["kg"] == 80.0


def test_other_muscle_raws_lists_only_unmapped_names():
    client = FakeSpeedianceClient(ROOT / "tests" / "fixtures" / "speediance_gaps.json")
    history, _by_exercise = normalize_bundles(
        client.fetch_between(date(2026, 1, 1), date(2026, 12, 31)),
        generated_at=STAMP,
        source_status="ok",
        last_successful_sync=STAMP,
    )
    assert other_muscle_raws(history) == ["Forearms"]
    assert other_muscle_raws(
        {
            "sessions": [
                {
                    "exercise_breakdown": [
                        {"muscle_group": "chest", "muscle_group_raw": "Pecs"},
                        {"muscle_group": "other", "muscle_group_raw": "Forearms"},
                        {"muscle_group": "other", "muscle_group_raw": None},
                        {"muscle_group": "other", "muscle_group_raw": "  "},
                        {"muscle_group": "other", "muscle_group_raw": "Calves"},
                    ]
                }
            ]
        }
    ) == ["Calves", "Forearms"]


def test_sync_logs_other_muscle_raws(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("SPEEDIANCE_FAKE", "1")
    monkeypatch.setenv("SPEEDIANCE_FIXTURE", str(ROOT / "tests" / "fixtures" / "speediance_gaps.json"))
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T12:00:00-07:00")
    with caplog.at_level(logging.INFO, logger="speediance.sync"):
        assert sync(destination=tmp_path) == 0
    line = next(record.message for record in caplog.records if "mapped to other" in record.message)
    assert "Forearms" in line
    assert "Pecs" not in line
