"""A failed sync keeps the last good sessions and marks the source stale."""

from __future__ import annotations

import json

from scripts.sync_speediance import sync
from tests.helpers import validate_schema


class ExplodingClient:
    def __init__(self, message: str) -> None:
        self.region = "Global"
        self._message = message

    def fetch_between(self, start, end):
        raise RuntimeError(self._message)


def test_failed_sync_preserves_sessions_and_redacts_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_FAKE", "1")
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T20:30:00-07:00")
    monkeypatch.setenv("SPEEDIANCE_START_DATE", "2026-01-01")
    monkeypatch.setenv("SPEEDIANCE_TOKEN", "super-secret-token-value")
    monkeypatch.setenv("SPEEDIANCE_USER_ID", "user-should-not-leak")
    monkeypatch.delenv("SPEEDIANCE_OUTPUT_DIR", raising=False)

    assert sync(tmp_path) == 0
    history_path = tmp_path / "speediance" / "normalized" / "history.json"
    before = json.loads(history_path.read_text(encoding="utf-8"))
    raw_before = (tmp_path / "speediance" / "raw" / "monthly" / "2026-09.json").read_bytes()
    assert before["source_status"] == "ok"
    assert before["last_successful_sync"] == "2026-10-14T20:30:00-07:00"
    validate_schema(before, "history.schema.json")
    validate_schema(json.loads((tmp_path / "status.json").read_text()), "status.schema.json")

    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T21:00:00-07:00")
    monkeypatch.setattr(
        "scripts.sync_speediance.SpeedianceClient.from_env",
        lambda: ExplodingClient("network down super-secret-token-value"),
    )
    assert sync(tmp_path) == 1

    after = json.loads(history_path.read_text(encoding="utf-8"))
    by_exercise = json.loads((tmp_path / "speediance" / "normalized" / "by_exercise.json").read_text())
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert after["sessions"] == before["sessions"]
    assert after["source_status"] == "stale"
    assert after["last_successful_sync"] == "2026-10-14T20:30:00-07:00"
    assert after["generated_at"] == "2026-10-14T21:00:00-07:00"
    assert by_exercise["source_status"] == "stale"
    assert by_exercise["last_successful_sync"] == "2026-10-14T20:30:00-07:00"
    assert status["sources"]["speediance"]["status"] == "stale"
    assert status["sources"]["speediance"]["last_successful_sync"] == "2026-10-14T20:30:00-07:00"
    assert status["sources"]["speediance"]["error"] == "network down [redacted]"
    blob = json.dumps({"history": after, "by_exercise": by_exercise, "status": status})
    assert "super-secret-token-value" not in blob
    assert "user-should-not-leak" not in blob
    assert (tmp_path / "speediance" / "raw" / "monthly" / "2026-09.json").read_bytes() == raw_before
    assert list(tmp_path.rglob("*.tmp")) == []
    validate_schema(after, "history.schema.json")
    validate_schema(by_exercise, "by_exercise.schema.json")
    validate_schema(status, "status.schema.json")


def test_first_failure_writes_status_without_inventing_history(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEEDIANCE_TOKEN", "super-secret-token-value")
    monkeypatch.setattr(
        "scripts.sync_speediance.SpeedianceClient.from_env",
        lambda: ExplodingClient("offline super-secret-token-value"),
    )
    monkeypatch.setenv("SPEEDIANCE_NOW", "2026-10-14T20:30:00-07:00")
    assert sync(tmp_path) == 1
    assert not (tmp_path / "speediance" / "normalized" / "history.json").exists()
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["sources"]["speediance"]["status"] == "stale"
    assert status["sources"]["speediance"]["last_successful_sync"] is None
    assert "super-secret-token-value" not in json.dumps(status)
    validate_schema(status, "status.schema.json")


def test_client_error_does_not_echo_headers(monkeypatch):
    import requests

    from scripts.speediance_client import SpeedianceClient

    class Response:
        status_code = 200

        def json(self):
            return {"code": 91, "message": "rejected super-secret-token-value"}

    class Session:
        def get(self, url, headers, timeout):
            assert headers["Token"] == "super-secret-token-value"
            assert "super-secret-token-value" not in url
            self.headers = headers
            return Response()

    monkeypatch.setenv("SPEEDIANCE_TOKEN", "super-secret-token-value")
    client = SpeedianceClient("user-1", "super-secret-token-value", "Global", session=Session())
    try:
        client.get_training_records("2026-10-01", "2026-10-14")
    except Exception as exc:
        message = str(exc)
    else:
        raise AssertionError("expected an auth error")
    assert "super-secret-token-value" not in message
    assert "App_user_id" not in message
    assert "Token" not in message
    assert "user-1" not in repr(client)
    assert isinstance(client.session, Session)
    assert not isinstance(client.session, requests.Session)
