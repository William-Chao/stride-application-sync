"""Request pacing, rate-limit backoff, profile weight unit, and the sync window."""

from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from scripts.speediance_client import (
    SpeedianceAuthError,
    SpeedianceClient,
    SpeedianceRateLimitError,
    request_delay_seconds,
)
from scripts.sync_speediance import resolve_start_date, sync

LA = ZoneInfo("America/Los_Angeles")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, body: dict | str, status_code: int = 200) -> None:
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
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


def _client(session, clock: Clock, delay: float = 1.5) -> SpeedianceClient:
    return SpeedianceClient(
        "user-1",
        "token-value-not-logged",
        session=session,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
        request_delay=delay,
    )


def _ok(data) -> Response:
    return Response({"code": 0, "data": data})


def _limited(code: int = 1001) -> Response:
    return Response({"code": code, "message": "Request too frequent, please try again later"})


def test_paces_between_monthly_calls_and_session_detail():
    clock = Clock()
    record = {
        "trainingId": 7,
        "type": 5,
        "title": "Upper",
        "trainingTime": 60,
        "calorie": 1,
        "startTimestamp": 1791853500,
    }
    session = ScriptedSession(
        [
            _ok([]),
            _ok([record]),
            _ok([]),
            _ok([]),
            _ok([]),
            _ok([]),
            _ok({}),
        ]
    )
    client = _client(session, clock)
    bundles = client.fetch_between(date(2026, 9, 1), date(2026, 10, 14))
    assert len(bundles) == 1
    assert bundles[0]["detail_available"] is False
    paths = [call.split("speediance.com", 1)[-1] for call in session.calls]
    assert paths == [
        "/api/mobile/v2/report/userTrainingDataRecord?startDate=2026-09-01&endDate=2026-09-30",
        "/api/mobile/v2/report/userTrainingDataRecord?startDate=2026-10-01&endDate=2026-10-14",
        "/api/app/trainingInfo/cttTrainingInfoDetail/7",
        "/api/app/trainingInfo/planTrainingInfoDetail/7",
        "/api/app/trainingInfo/courseTrainingInfoDetail/7",
        "/api/app/trainingInfo/freeTrainingDetail/7",
        "/api/app/trainingInfo/cttTrainingInfo/7",
    ]
    assert clock.sleeps == [1.5, 1.5, 1.5, 1.5, 1.5, 1.5]
    assert not any("/v2/trainingInfo/" in call or "/v3/" in call for call in session.calls)


def test_rate_limit_backs_off_then_succeeds_without_an_extra_gap():
    clock = Clock()
    session = ScriptedSession([_limited(), _limited(), _ok([])])
    client = _client(session, clock)
    assert client.fetch_between(date(2026, 10, 1), date(2026, 10, 14)) == []
    assert clock.sleeps == [10, 30]
    assert len(session.calls) == 3


def test_rate_limit_retries_at_most_three_times():
    clock = Clock()
    session = ScriptedSession([_limited(), _limited(), Response({"code": 429, "message": "slow down"}, status_code=429), _limited()])
    client = _client(session, clock, delay=0)
    try:
        client.get_training_records("2026-10-01", "2026-10-14")
    except SpeedianceRateLimitError as exc:
        assert "too frequent" in str(exc).lower()
    else:
        raise AssertionError("expected the rate limit to fail closed")
    assert clock.sleeps == [10, 30, 60]
    assert len(session.calls) == 4


def test_auth_errors_are_not_retried(monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_TOKEN", "token-value-not-logged")
    clock = Clock()
    session = ScriptedSession(
        [Response({"code": 91, "message": "Request too frequent, please try again later token-value-not-logged"})]
    )
    client = _client(session, clock)
    try:
        client.get_training_records("2026-10-01", "2026-10-14")
    except SpeedianceAuthError as exc:
        assert "token-value-not-logged" not in str(exc)
    else:
        raise AssertionError("expected an auth error")
    assert clock.sleeps == []
    assert len(session.calls) == 1

    clock = Clock()
    session = ScriptedSession([Response({"message": "nope"}, status_code=401)])
    client = _client(session, clock)
    try:
        client.get_training_records("2026-10-01", "2026-10-14")
    except SpeedianceAuthError:
        pass
    else:
        raise AssertionError("expected an auth error")
    assert clock.sleeps == []
    assert len(session.calls) == 1


def test_profile_weight_unit_overrides_env_and_falls_back():
    clock = Clock()
    session = ScriptedSession([_ok({"weightUnit": 1})])
    client = _client(session, clock, delay=0)
    client.weight_unit = "kg"
    client.refresh_weight_unit()
    assert client.weight_unit == "lb"

    session = ScriptedSession([_ok({"name": "Test User"})])
    client = _client(session, clock, delay=0)
    client.refresh_weight_unit()
    assert client.weight_unit == "kg"

    session = ScriptedSession([Response({"code": 404, "message": "missing"})])
    client = _client(session, clock, delay=0)
    client.refresh_weight_unit()
    assert client.weight_unit == "kg"
    assert clock.sleeps == []

    session = ScriptedSession([_ok({"appUserInfo": {"weightUnit": 1}})])
    client = _client(session, clock, delay=0)
    client.weight_unit = "kg"
    client.refresh_weight_unit()
    assert client.weight_unit == "lb"

    session = ScriptedSession([_ok({"userinfo": {"unit": 1, "appUserId": 5}})])
    client = _client(session, clock, delay=0)
    client.weight_unit = "kg"
    client.refresh_weight_unit()
    assert client.weight_unit == "lb"

    session = ScriptedSession([_ok({"device": {"unit": 1}})])
    client = _client(session, clock, delay=0)
    client.weight_unit = "kg"
    client.refresh_weight_unit()
    assert client.weight_unit == "kg"


def test_request_delay_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_REQUEST_DELAY_S", "2.5")
    assert request_delay_seconds() == 2.5


def test_exhausted_rate_limit_marks_the_source_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T20:30:00-07:00")
    monkeypatch.setenv("SPEEDIANCE_START_DATE", "2026-10-01")
    monkeypatch.delenv("SPEEDIANCE_FAKE", raising=False)
    clock = Clock()
    session = ScriptedSession([_limited() for _ in range(4)])
    client = _client(session, clock, delay=0)
    monkeypatch.setattr("scripts.sync_speediance.SpeedianceClient.from_env", lambda: client)
    assert sync(tmp_path) == 1
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["sources"]["speediance"]["status"] == "stale"
    assert "too frequent" in status["sources"]["speediance"]["error"].lower()
    assert "token-value-not-logged" not in json.dumps(status)
    assert clock.sleeps == [10, 30, 60]
    assert not (tmp_path / "speediance" / "normalized" / "history.json").exists()


def test_default_window_is_twelve_months_then_incremental(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEEDIANCE_START_DATE", raising=False)
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T20:30:00-07:00")
    assert resolve_start_date(tmp_path, date(2026, 10, 14)) == date(2025, 10, 14)

    status = {
        "sources": {
            "speediance": {
                "status": "ok",
                "last_successful_sync": "2026-09-18T20:30:00-07:00",
                "last_attempt": "2026-09-18T20:30:00-07:00",
                "error": None,
            }
        }
    }
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert resolve_start_date(tmp_path, date(2026, 10, 14)) == date(2026, 8, 1)

    monkeypatch.setenv("SPEEDIANCE_START_DATE", "2024-02-01")
    assert resolve_start_date(tmp_path, date(2026, 10, 14)) == date(2024, 2, 1)


def test_incremental_sync_keeps_months_before_the_window(tmp_path, monkeypatch):
    monkeypatch.delenv("SPEEDIANCE_START_DATE", raising=False)
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T20:30:00-07:00")
    monkeypatch.delenv("SPEEDIANCE_FAKE", raising=False)
    july_ts = int(datetime(2026, 7, 15, 18, 0, tzinfo=LA).timestamp())
    october_ts = 1791853500
    monthly = tmp_path / "speediance" / "raw" / "monthly"
    monthly.mkdir(parents=True)
    (monthly / "2026-07.json").write_text(
        json.dumps(
            {
                "month": "2026-07",
                "records": [
                    {
                        "summary": {
                            "trainingId": 1,
                            "title": "July",
                            "type": 1,
                            "trainingTime": 600,
                            "calorie": 40,
                            "startTimestamp": july_ts,
                        },
                        "session": {},
                        "detail": [],
                        "source_weight_unit": "kg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "status.json").write_text(
        json.dumps(
            {
                "sources": {
                    "speediance": {
                        "status": "ok",
                        "last_successful_sync": "2026-09-18T20:30:00-07:00",
                        "last_attempt": "2026-09-18T20:30:00-07:00",
                        "error": None,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class Recorder:
        region = "Global"
        weight_unit = "kg"

        def fetch_between(self, start, end):
            self.window = (start, end)
            return [
                {
                    "summary": {
                        "trainingId": 2,
                        "title": "October",
                        "type": 1,
                        "trainingTime": 600,
                        "calorie": 50,
                        "startTimestamp": october_ts,
                    },
                    "session": {},
                    "detail": [],
                    "source_weight_unit": "kg",
                }
            ]

    recorder = Recorder()
    monkeypatch.setattr("scripts.sync_speediance.SpeedianceClient.from_env", lambda: recorder)
    assert sync(tmp_path) == 0
    assert recorder.window == (date(2026, 8, 1), date(2026, 10, 14))
    history = json.loads((tmp_path / "speediance" / "normalized" / "history.json").read_text(encoding="utf-8"))
    assert [session["training_id"] for session in history["sessions"]] == ["1", "2"]
    assert (monthly / "2026-07.json").is_file()
    assert (monthly / "2026-10.json").is_file()
