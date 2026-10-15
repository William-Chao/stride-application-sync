"""Detail probe URL plan, redaction, and capture headers. No live network."""

from __future__ import annotations

import json
from datetime import date

from scripts.detail_probe import (
    build_report,
    collect_probes,
    format_capture,
    lookup_windows,
    probe_targets,
)
from scripts.speediance_client import SpeedianceAuthError, SpeedianceClient, SpeedianceRateLimitError


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
        self.headers: list[dict] = []

    def get(self, url, headers, timeout):
        self.calls.append(url)
        self.headers.append(dict(headers))
        if not self.responses:
            raise AssertionError(f"unexpected request {url}")
        return self.responses.pop(0)


def _client(session: ScriptedSession) -> SpeedianceClient:
    return SpeedianceClient(
        "user-1",
        "token-value-not-logged",
        session=session,
        sleeper=lambda _seconds: None,
        monotonic=lambda: 0.0,
        request_delay=0,
    )


def test_probe_plan_uses_plan_detail_and_skips_dead_hosts():
    record = {
        "trainingId": 670355,
        "id": 7820708,
        "courseId": 2144,
        "templateId": 42,
        "type": 1,
        "courseType": 0,
        "totalCapacity": 8028,
        "title": "Strength",
    }
    targets = probe_targets("670355", record=record, region="Global")
    paths = [item["path"] for item in targets]
    assert paths[0] == "/api/app/trainingInfo/planTrainingInfoDetail/670355"
    assert "/api/app/trainingInfo/planTrainingInfoDetail/7820708" not in paths
    assert "/api/app/trainingInfo/freeTrainingDetail/670355" in paths
    assert "/api/app/trainingInfo/freeTrainingDetail/42" in paths
    assert paths[-1] == "/api/mobile/muscleLoad/detail?trainingId=670355"
    assert all(item["host"] == "api2.speediance.com" for item in targets)
    assert all(item["versioncode"] == "40304" for item in targets)
    blob = "\n".join(paths)
    assert "/v2/" not in blob
    assert "/v3/" not in blob
    assert "api3" not in blob


def test_custom_probe_asks_ctt_before_plan():
    targets = probe_targets("22", record={"trainingId": 22, "type": 5}, region="Global")
    paths = [item["path"] for item in targets]
    assert paths[0] == "/api/app/trainingInfo/cttTrainingInfoDetail/22"
    assert paths[1] == "/api/app/trainingInfo/cttTrainingInfo/22"
    assert paths[2] == "/api/app/trainingInfo/planTrainingInfoDetail/22"


def test_lookup_windows_use_the_session_month_or_the_last_two():
    assert lookup_windows(date(2026, 10, 13), date(2026, 10, 14)) == [(date(2026, 10, 1), date(2026, 10, 14))]
    assert lookup_windows(date(2026, 9, 25), date(2026, 10, 14)) == [(date(2026, 9, 1), date(2026, 9, 30))]
    assert lookup_windows(None, date(2026, 10, 14)) == [
        (date(2026, 10, 1), date(2026, 10, 14)),
        (date(2026, 9, 1), date(2026, 9, 30)),
    ]


def test_capture_redacts_the_token_and_omits_headers(monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_TOKEN", "probe-secret-token-value")
    session = ScriptedSession(
        [
            Response(
                {
                    "code": 0,
                    "message": "ok probe-secret-token-value",
                    "data": {
                        "Token": "probe-secret-token-value",
                        "finishedReps": [{"finishedCount": 5}],
                        "actionLibraryName": "Squat",
                        "note": "probe-secret-token-value",
                    },
                }
            )
        ]
    )
    client = _client(session)
    captured = client.capture(
        "https://api3.speediance.com/api/app/trainingInfo/freeTrainingDetail/670355",
        versioncode="42200",
    )
    target = {
        "label": "api3 free detail 670355",
        "path": "/api/app/trainingInfo/freeTrainingDetail/670355",
        "host": "api3.speediance.com",
        "versioncode": "42200",
    }
    row = format_capture(target, captured)
    blob = json.dumps(row)
    assert "probe-secret-token-value" not in blob
    assert "headers" not in row
    assert row["shape"]["has_finishedReps"] is True
    assert row["shape"]["has_actionLibraryName"] is True
    assert row["body"]["data"]["Token"] == "[redacted]"
    assert row["message"] == "ok [redacted]"
    assert session.headers[0]["Host"] == "api3.speediance.com"
    assert session.headers[0]["Versioncode"] == "42200"
    report = build_report(
        "670355",
        lookup_date="2026-10-13",
        summary={
            "Token": "probe-secret-token-value",
            "trainingId": 670355,
            "totalCapacity": 8028,
            "title": "Strength",
        },
        probes=[row],
        stopped=None,
    )
    assert "probe-secret-token-value" not in json.dumps(report)
    assert report["summary"]["totalCapacity"] == 8028
    assert "Token" not in report["summary"]
    assert "headers" not in report


def test_auth_on_the_account_host_stops_and_a_foreign_host_is_a_miss():
    class Stub:
        def __init__(self, outcomes):
            self.base_url = "https://api2.speediance.com"
            self.outcomes = list(outcomes)
            self.calls: list[str] = []

        def capture(self, url, *, versioncode=None):
            self.calls.append(url)
            item = self.outcomes.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    targets = [
        {"label": "v1", "path": "/api/app/trainingInfo/freeTrainingDetail/1", "host": "api2.speediance.com", "versioncode": "40304", "speculative": False},
        {"label": "next", "path": "/api/app/trainingInfo/freeTraining/1", "host": "api2.speediance.com", "versioncode": "40304", "speculative": False},
    ]
    stub = Stub([SpeedianceAuthError("Speediance unauthorized")])
    probes, stopped = collect_probes(stub, targets)
    assert stopped == "auth"
    assert len(probes) == 1
    assert len(stub.calls) == 1
    assert probes[0]["error"] == "auth"
    assert "headers" not in probes[0]

    targets = [
        {"label": "api3", "path": "/api/app/trainingInfo/planTrainingInfoDetail/670355", "host": "api3.speediance.com", "versioncode": "40304", "speculative": False},
        {"label": "v1", "path": "/api/app/trainingInfo/planTrainingInfoDetail/670355", "host": "api2.speediance.com", "versioncode": "40304", "speculative": False},
    ]
    stub = Stub(
        [
            SpeedianceAuthError("Login expired"),
            {"http_status": 200, "code": 0, "message": "", "data": [], "body": {"code": 0, "data": []}},
        ]
    )
    probes, stopped = collect_probes(stub, targets)
    assert stopped is None
    assert len(probes) == 2
    assert probes[0]["error"] == "miss"
    assert probes[0]["message"] == "Login expired"
    assert probes[1]["http_status"] == 200

    stub = Stub([SpeedianceRateLimitError("Speediance rate limit: Request too frequent, please try again later")])
    probes, stopped = collect_probes(stub, targets[:1])
    assert stopped == "rate_limit"
    assert probes[0]["error"] == "rate_limit"
