"""Minimal Speediance client for training history and session detail.

Auth is the captured app user id plus token sent as ``App_user_id`` and
``Token``. Region ``Global`` uses https://api2.speediance.com. Region ``EU``
uses https://euapi.speediance.com.

Endpoint paths and the header names follow the MIT-licensed unofficial
clients. This file is a new, smaller client for read-only history sync.
It does not log headers or secrets.

Sources, all MIT License, Copyright (c) 2025 hbui3:

- https://github.com/hbui3/UnofficialSpeedianceWorkoutManager
- https://github.com/ANPC86/SmartGymWorkoutManager
- https://github.com/clawdassistant85-netizen/speediance-smartgym-workout-manager
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from scripts.constants import DEFAULT_REQUEST_DELAY_S, LA, RATE_LIMIT_BACKOFF_S, ROOT
from scripts.envutil import load_dotenv
from scripts.redact import public_error, redact
from scripts.timeutil import now_la, utc_offset_hhmm

logger = logging.getLogger("speediance.client")

REGION_HOSTS = {
    "global": "api2.speediance.com",
    "eu": "euapi.speediance.com",
}

# Per-set detail for a performed plan lives at planTrainingInfoDetail, keyed by
# trainingId. Custom templates (type 5) still prefer cttTrainingInfoDetail.
# The older free, course, and ctt namespaces stay as fallbacks. v2, v3, and
# api3 do not return rows for this account. An empty or error response tries
# the next path. A different record id is tried only when trainingId produced
# no performed sets.
NAMESPACE_NAMES = {
    "ctt": ("cttTrainingInfoDetail", "cttTrainingInfo"),
    "course": ("courseTrainingInfoDetail", "courseTrainingInfo"),
    "free": ("freeTrainingDetail", "freeTraining"),
}
NAMESPACES = {
    kind: (
        f"/api/app/trainingInfo/{detail}/{{training_id}}",
        f"/api/app/trainingInfo/{info}/{{training_id}}",
    )
    for kind, (detail, info) in NAMESPACE_NAMES.items()
}
ALTERNATE_ID_KEYS = ("id", "uuid", "trainingInfoId", "dataId", "recordId")
DEFAULT_VERSIONCODE = "40304"

EXERCISE_LIST_KEYS = (
    "actionLibraryList",
    "cttActionLibraryTrainingInfoList",
    "trainingInfoList",
    "finishedActionList",
    "list",
    "records",
    "detail",
    "data",
)

MUSCLE_NAME_KEYS = (
    "mainMuscleGroupName",
    "trainingPartName",
    "muscleGroupName",
    "muscleGroup",
    "muscle_group",
    "partName",
)
GROUP_ID_KEYS = ("actionLibraryGroupId", "groupId")
LIBRARY_CHUNK = 50

_MISS = object()
_PROFILE_HINTS = {
    "appuserid",
    "userid",
    "username",
    "email",
    "sex",
    "height",
    "birthday",
    "weight",
    "useridentity",
}


class SpeedianceError(RuntimeError):
    """Base error. Messages must stay free of tokens and headers."""


class SpeedianceAuthError(SpeedianceError):
    """Missing credentials or a rejected token."""


class SpeedianceAPIError(SpeedianceError):
    """The history or detail call failed."""


class SpeedianceRateLimitError(SpeedianceAPIError):
    """The API asked us to slow down, including after the retry budget."""


def request_delay_seconds() -> float:
    raw = os.environ.get("SPEEDIANCE_REQUEST_DELAY_S", "").strip()
    if not raw:
        return DEFAULT_REQUEST_DELAY_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise SpeedianceAPIError("SPEEDIANCE_REQUEST_DELAY_S must be a number of seconds") from exc
    if value < 0:
        raise SpeedianceAPIError("SPEEDIANCE_REQUEST_DELAY_S must be >= 0")
    return value


def is_rate_limited(status_code: int, code: int | None, message: str) -> bool:
    """True for HTTP 429, the too-frequent message, or that message's API code."""
    if status_code == 429 or code == 429:
        return True
    text = (message or "").lower()
    if "too frequent" in text or "过于频繁" in text:
        return True
    return False


def parse_weight_unit(value: str | None) -> str:
    if value is None or str(value).strip() == "":
        return "kg"
    text = str(value).strip().lower()
    if text in {"1", "lb", "lbs", "imperial"}:
        return "lb"
    if text in {"0", "kg", "kgs", "metric"}:
        return "kg"
    raise SpeedianceAPIError("SPEEDIANCE_UNIT must be 0 (kg) or 1 (lb)")


def normalize_region(region: str | None) -> str:
    text = (region or "Global").strip() or "Global"
    key = text.lower()
    if key not in REGION_HOSTS:
        raise SpeedianceAPIError("SPEEDIANCE_REGION must be Global or EU")
    return "EU" if key == "eu" else "Global"


class SpeedianceClient:
    """Read-only client. ``repr`` never includes the token."""

    def __init__(
        self,
        user_id: str,
        token: str,
        region: str = "Global",
        weight_unit: str = "kg",
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        request_delay: float | None = None,
    ) -> None:
        self.region = normalize_region(region)
        self.weight_unit = weight_unit if weight_unit in {"kg", "lb"} else parse_weight_unit(weight_unit)
        self._user_id = user_id
        self._token = token
        host = REGION_HOSTS[self.region.lower()]
        self.base_url = f"https://{host}"
        self.session = session or requests.Session()
        self._sleeper = sleeper or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._request_delay = DEFAULT_REQUEST_DELAY_S if request_delay is None else request_delay
        self._last_request_at: float | None = None
        self._muscle_names: dict[str, str | None] = {}

    def __repr__(self) -> str:
        return f"SpeedianceClient(region={self.region})"

    @classmethod
    def from_env(cls) -> SpeedianceClient | FakeSpeedianceClient:
        load_dotenv()
        if os.environ.get("SPEEDIANCE_FAKE", "").strip() == "1":
            fixture = os.environ.get("SPEEDIANCE_FIXTURE", "").strip()
            path = Path(fixture) if fixture else ROOT / "tests" / "fixtures" / "speediance_payload.json"
            return FakeSpeedianceClient(path)
        missing = [
            name
            for name in ("SPEEDIANCE_USER_ID", "SPEEDIANCE_TOKEN")
            if not os.environ.get(name, "").strip()
        ]
        if missing:
            raise SpeedianceAuthError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        return cls(
            user_id=os.environ["SPEEDIANCE_USER_ID"].strip(),
            token=os.environ["SPEEDIANCE_TOKEN"].strip(),
            region=os.environ.get("SPEEDIANCE_REGION", "Global"),
            weight_unit=parse_weight_unit(os.environ.get("SPEEDIANCE_UNIT")),
            request_delay=request_delay_seconds(),
        )

    def _headers(self, *, versioncode: str | None = None, host: str | None = None) -> dict[str, str]:
        moment = now_la()
        return {
            "Host": host or self.base_url.removeprefix("https://"),
            "Timestamp": str(int(moment.timestamp() * 1000)),
            "Versioncode": versioncode or DEFAULT_VERSIONCODE,
            "Content-Type": "application/json",
            "User-Agent": "Dart/3.9 (dart:io)",
            "App_type": "SOFTWARE",
            "Timezone": "America/Los_Angeles",
            "Utc_offset": utc_offset_hhmm(moment),
            "Accept-Language": "en",
            "App_user_id": self._user_id,
            "Token": self._token,
        }

    def _pace(self) -> None:
        if self._last_request_at is None or self._request_delay <= 0:
            return
        remaining = self._request_delay - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleeper(remaining)

    def _get_data(self, url: str) -> Any:
        retries = len(RATE_LIMIT_BACKOFF_S)
        for attempt in range(retries + 1):
            self._pace()
            self._last_request_at = self._monotonic()
            try:
                response = self.session.get(url, headers=self._headers(), timeout=30)
            except requests.RequestException as exc:
                raise SpeedianceAPIError(redact(f"Speediance request failed ({exc.__class__.__name__})")) from None
            outcome = _interpret_response(response)
            if outcome[0] == "auth":
                raise SpeedianceAuthError(outcome[1])
            if outcome[0] == "rate_limit":
                if attempt >= retries:
                    raise SpeedianceRateLimitError(outcome[1])
                delay = RATE_LIMIT_BACKOFF_S[attempt]
                logger.warning("Speediance rate limited, retrying in %ss (%s/%s)", delay, attempt + 1, retries)
                self._sleeper(delay)
                continue
            if outcome[0] == "error":
                raise SpeedianceAPIError(outcome[1])
            return outcome[1]
        raise SpeedianceRateLimitError("Speediance rate limit: Request too frequent, please try again later")

    def capture(self, url: str, *, versioncode: str | None = None) -> dict:
        """Paced GET that returns the raw envelope instead of raising on an API error.

        Auth failures and an exhausted rate-limit budget still raise. The result
        has ``http_status``, ``code``, ``message``, ``data``, and ``body``. It
        never includes request headers.
        """
        retries = len(RATE_LIMIT_BACKOFF_S)
        host = urlsplit(url).hostname or ""
        for attempt in range(retries + 1):
            self._pace()
            self._last_request_at = self._monotonic()
            try:
                response = self.session.get(
                    url,
                    headers=self._headers(versioncode=versioncode, host=host),
                    timeout=30,
                )
            except requests.RequestException as exc:
                raise SpeedianceAPIError(redact(f"Speediance request failed ({exc.__class__.__name__})")) from None
            status = response.status_code
            body, raw_text = _response_body(response)
            code = None
            if isinstance(body, dict):
                code = _as_int(body.get("code"))
                message = redact(str(body.get("message") or ""))
            else:
                message = redact(raw_text)
            if status == 401 or code == 91:
                raise SpeedianceAuthError(message or "Speediance unauthorized")
            if is_rate_limited(status, code, message):
                if attempt >= retries:
                    raise SpeedianceRateLimitError(
                        "Speediance rate limit: Request too frequent, please try again later"
                    )
                delay = RATE_LIMIT_BACKOFF_S[attempt]
                logger.warning("Speediance rate limited, retrying in %ss (%s/%s)", delay, attempt + 1, retries)
                self._sleeper(delay)
                continue
            return {
                "http_status": status,
                "code": code,
                "message": message,
                "data": body.get("data") if isinstance(body, dict) else None,
                "body": body if body is not None else raw_text[:300],
            }
        raise SpeedianceRateLimitError("Speediance rate limit: Request too frequent, please try again later")

    def refresh_weight_unit(self) -> None:
        """Prefer the account weightUnit. Keep the env unit when the profile has none."""
        fallback = self.weight_unit
        try:
            data = self._get_data(f"{self.base_url}/api/app/userinfo")
        except SpeedianceAuthError:
            raise
        except SpeedianceRateLimitError:
            raise
        except SpeedianceAPIError:
            logger.info("Speediance profile did not provide a weight unit; using %s", fallback)
            return
        found = extract_weight_unit(data)
        if found:
            self.weight_unit = found
            logger.info("Speediance account weight unit is %s", found)
        else:
            logger.info("Speediance profile did not provide a weight unit; using %s", fallback)

    def get_training_records(self, start_date: str, end_date: str) -> list[dict]:
        url = (
            f"{self.base_url}/api/mobile/v2/report/userTrainingDataRecord"
            f"?startDate={start_date}&endDate={end_date}"
        )
        data = self._get_data(url)
        if data is None:
            return []
        if not isinstance(data, list):
            raise SpeedianceAPIError("Training history payload was not a list")
        return [row for row in data if isinstance(row, dict)]

    def get_training_detail(self, training_id: str, training_type: int | None) -> list[dict]:
        detail, _session, _available = self._fetch_detail(training_id, training_type)
        return detail

    def fetch_between(self, start: date, end: date) -> list[dict]:
        """Return raw bundles for each session in the inclusive date range.

        A bundle is ``{summary, session, detail, source_weight_unit, detail_available}``.
        ``summary`` and ``detail`` keep the vendor field names.
        """
        bundles: list[dict] = []
        for month_start, month_end in iter_months(start, end):
            records = self.get_training_records(month_start.isoformat(), month_end.isoformat())
            for record in records:
                bundles.append(self._bundle_record(record))
        self._attach_muscle_names(bundles)
        self._attach_muscle_load(bundles)
        return bundles

    def _bundle_record(self, record: dict) -> dict:
        training_id = primary_training_id(record)
        training_type = _as_int(record.get("type"))
        detail, session, available = self._fetch_detail(training_id, training_type, record)
        return {
            "summary": record,
            "session": session,
            "detail": detail,
            "detail_available": available,
            "source_weight_unit": _record_weight_unit(record, self.weight_unit),
        }

    def _fetch_detail(
        self,
        training_id: str,
        training_type: int | None,
        record: dict | None = None,
    ) -> tuple[list[dict], dict, bool]:
        """Return exercise rows, the matching info object, and detail_available.

        Plan detail is tried before the older namespaces for every type except
        custom templates, which still try ctt first. A payload with performed
        sets stops the search. An empty list or an API error tries the next
        path. ``id``, ``uuid``, ``trainingInfoId``, ``dataId``, and ``recordId``
        are tried only when ``trainingId`` produced no performed sets. The info
        call belongs to the namespace that had rows, or to the first empty
        namespace that has an info endpoint.
        """
        identities = session_identity_ids(training_id, record)
        preferred = detail_candidates(training_type, identities[0])[0][0]
        first_info_url: str | None = None
        for index, ident in enumerate(identities):
            if index:
                logger.info("Speediance detail retrying with alternate id %s", ident)
            for detail_path, info_path in detail_candidates(training_type, ident):
                payload = self._get_optional(self.base_url + detail_path, log_miss=True)
                if payload is _MISS:
                    continue
                exercises = coerce_exercises(payload)
                if exercises and has_performed_sets(exercises):
                    if detail_path != preferred:
                        logger.info("Speediance detail rows found path=%s", detail_path)
                    session: dict = {}
                    if info_path:
                        info_payload = self._get_optional(self.base_url + info_path)
                        if isinstance(info_payload, dict):
                            session = info_payload
                    return exercises, session, True
                if first_info_url is None and info_path:
                    first_info_url = self.base_url + info_path
        if first_info_url is not None:
            info_payload = self._get_optional(first_info_url)
            session = info_payload if isinstance(info_payload, dict) else {}
            return [], session, False
        return [], {}, False

    def _get_optional(self, url: str, *, log_miss: bool = False) -> Any:
        try:
            return self._get_data(url)
        except SpeedianceAuthError:
            raise
        except SpeedianceRateLimitError:
            raise
        except SpeedianceAPIError as exc:
            if log_miss:
                logger.info(
                    "Speediance detail namespace had no payload path=%s message=%s",
                    _url_path(url),
                    public_error(exc),
                )
            return _MISS

    def _attach_muscle_names(self, bundles: list[dict]) -> None:
        """Fill mainMuscleGroupName from the action library when the set payload has none."""
        needed: list[str] = []
        seen: set[str] = set()
        for bundle in bundles:
            for exercise in bundle.get("detail") or []:
                if not isinstance(exercise, dict) or _muscle_text(exercise):
                    continue
                group_id = _group_id(exercise)
                if group_id and group_id not in self._muscle_names and group_id not in seen:
                    seen.add(group_id)
                    needed.append(group_id)
        for offset in range(0, len(needed), LIBRARY_CHUNK):
            chunk = needed[offset : offset + LIBRARY_CHUNK]
            query = "&".join(f"ids={group_id}" for group_id in chunk)
            payload = self._get_optional(f"{self.base_url}/api/app/actionLibraryGroup/list?{query}")
            if payload is _MISS:
                for group_id in chunk:
                    self._muscle_names.setdefault(group_id, None)
                continue
            for row in _library_rows(payload):
                group_id = _group_id(row) or _text_id(row.get("id"))
                if not group_id:
                    continue
                self._muscle_names[group_id] = _muscle_text(row)
            for group_id in chunk:
                self._muscle_names.setdefault(group_id, None)
        for bundle in bundles:
            for exercise in bundle.get("detail") or []:
                if not isinstance(exercise, dict) or _muscle_text(exercise):
                    continue
                group_id = _group_id(exercise)
                if not group_id:
                    continue
                name = self._muscle_names.get(group_id)
                if name:
                    exercise["mainMuscleGroupName"] = name

    def _attach_muscle_load(self, bundles: list[dict]) -> None:
        """Session-level muscle names when the set rows did not name a muscle."""
        for bundle in bundles:
            exercises = [row for row in (bundle.get("detail") or []) if isinstance(row, dict)]
            if not exercises or any(_muscle_text(exercise) for exercise in exercises):
                continue
            training_id = _text_id((bundle.get("summary") or {}).get("trainingId"))
            if not training_id:
                continue
            payload = self._get_optional(
                f"{self.base_url}/api/mobile/muscleLoad/detail?trainingId={training_id}"
            )
            names = muscle_load_names(payload)
            if names:
                bundle["muscle_load_names"] = names


class FakeSpeedianceClient:
    """Fixture client used when SPEEDIANCE_FAKE=1. It never opens a socket."""

    def __init__(self, fixture_path: Path) -> None:
        self.region = "Global"
        self.weight_unit = "kg"
        self.fixture_path = fixture_path
        self._payload: dict | None = None

    def __repr__(self) -> str:
        return "FakeSpeedianceClient()"

    def _load(self) -> dict:
        if self._payload is None:
            if not self.fixture_path.is_file():
                raise SpeedianceAPIError(f"Speediance fixture not found: {self.fixture_path.name}")
            import json

            self._payload = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            self.weight_unit = parse_weight_unit(self._payload.get("source_weight_unit"))
        return self._payload

    def fetch_between(self, start: date, end: date) -> list[dict]:
        payload = self._load()
        default_unit = parse_weight_unit(payload.get("source_weight_unit"))
        bundles = []
        for record in payload.get("records", []):
            summary = record.get("summary") or {}
            session_date = session_local_date(summary)
            if session_date is None or session_date < start or session_date > end:
                continue
            detail = coerce_exercises(record.get("detail"))
            if "detail_available" in record:
                available = bool(record.get("detail_available"))
            else:
                available = bool(detail)
            bundles.append(
                {
                    "summary": summary,
                    "session": record.get("session") or {},
                    "detail": detail,
                    "detail_available": available,
                    "source_weight_unit": _record_weight_unit(summary, default_unit),
                    "muscle_load_names": list(record.get("muscle_load_names") or []),
                }
            )
        return bundles


_SET_LIST_KEYS = ("finishedReps", "sets", "setList", "trainingSetList", "finishedSetList")
_FINISHED_REP_KEYS = ("finishedCount", "actualCount", "actualReps", "completeCount", "finishCount")
_MUSCLE_LOAD_KEYS = ("muscleDetailList", "muscleGroupList", "muscleList", "list", "records", "detail", "data")


def exercise_set_rows(exercise: dict) -> list[dict]:
    """Set dicts for one exercise. ``finishedReps`` wins over a target ``sets`` list."""
    for key in _SET_LIST_KEYS:
        value = exercise.get(key)
        if isinstance(value, list) and any(isinstance(item, dict) for item in value):
            return [item for item in value if isinstance(item, dict)]
    if any(key in exercise for key in ("finishedCount", "time", "duration", "trainingInfoDetail")):
        return [exercise]
    return []


def finished_rep_count(raw_set: dict) -> int:
    """Reps actually performed. Target reps are ignored."""
    for key in _FINISHED_REP_KEYS:
        count = _positive_number(raw_set.get(key))
        if count is not None:
            return count
    nested = raw_set.get("finishedReps")
    count = _positive_number(nested)
    if count is not None and not isinstance(nested, (list, dict)):
        return count
    return 0


def has_performed_sets(exercises: list[dict]) -> bool:
    """True when a payload has finished reps, a duration, or a per-rep weight trace."""
    for exercise in exercises:
        if not isinstance(exercise, dict):
            continue
        for raw_set in exercise_set_rows(exercise):
            if finished_rep_count(raw_set) > 0 or _set_duration(raw_set) > 0:
                return True
            if _explicit_unfinished(raw_set):
                continue
            if _weight_list(raw_set):
                return True
    return False


def muscle_load_names(data: Any) -> list[str]:
    """Muscle names from a muscleLoad totals payload. Order is preserved."""
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = []
        for key in _MUSCLE_LOAD_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            rows = [data]
    else:
        return []
    names: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = _muscle_text(row)
        if text and text not in names:
            names.append(text)
    return names


def _explicit_unfinished(raw_set: dict) -> bool:
    if "finishedCount" in raw_set and finished_rep_count(raw_set) <= 0:
        return True
    nested = raw_set.get("finishedReps")
    if isinstance(nested, (int, float)) and not isinstance(nested, bool) and int(nested) <= 0:
        return True
    return False


def _set_duration(raw_set: dict) -> int:
    for key in ("time", "duration", "trainTime"):
        count = _positive_number(raw_set.get(key))
        if count:
            return count
    detail = raw_set.get("trainingInfoDetail") or {}
    if isinstance(detail, dict):
        for key in ("time", "duration"):
            count = _positive_number(detail.get(key))
            if count:
                return count
    return 0


def _weight_list(raw_set: dict) -> list:
    detail = raw_set.get("trainingInfoDetail") or {}
    if isinstance(detail, dict):
        for key in ("weights", "leftWeights", "rightWeights"):
            value = detail.get(key)
            if isinstance(value, list) and value:
                return value
    return []


def _positive_number(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def coerce_exercises(data: Any) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        if "finishedReps" in data or "actionLibraryName" in data:
            return [data]
        for key in EXERCISE_LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return []
    raise SpeedianceAPIError("Exercise detail payload had an unexpected shape")


def iter_months(start: date, end: date) -> list[tuple[date, date]]:
    if end < start:
        return []
    months: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        month_end = next_month.fromordinal(next_month.toordinal() - 1)
        range_start = start if cursor < start else cursor
        range_end = end if month_end > end else month_end
        months.append((range_start, range_end))
        cursor = next_month
    return months


def session_local_date(summary: dict) -> date | None:
    moment = session_start(summary)
    if moment is None:
        return None
    return moment.astimezone(LA).date()


def session_start(summary: dict) -> datetime | None:
    raw_ts = summary.get("startTimestamp", summary.get("endTimestamp"))
    if raw_ts not in (None, ""):
        try:
            stamp = float(raw_ts)
        except (TypeError, ValueError):
            stamp = None
        if stamp is not None:
            if stamp > 10_000_000_000:
                stamp = stamp / 1000.0
            return datetime.fromtimestamp(stamp, tz=LA)
    raw_text = summary.get("startTime") or summary.get("endTime")
    if isinstance(raw_text, str) and raw_text.strip():
        parsed = datetime.fromisoformat(raw_text.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LA)
        return parsed.astimezone(LA)
    return None


def _record_weight_unit(record: dict, default: str) -> str:
    if "unit" in record and record.get("unit") not in (None, ""):
        return parse_weight_unit(record.get("unit"))
    if record.get("weightUnit"):
        return parse_weight_unit(record.get("weightUnit"))
    return default


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def detail_candidates(training_type: int | None, training_id: str) -> list[tuple[str, str | None]]:
    """Detail path and matching info path. Plan detail has no info endpoint.

    Types other than 5 ask for plan rows before the namespaces that come back
    empty for recent strength sessions. Type 5 still asks ctt first.
    """
    ident = str(training_id)
    plan = (f"/api/app/trainingInfo/planTrainingInfoDetail/{ident}", None)
    namespaced = []
    for kind in namespace_order(training_type):
        detail_path, info_path = namespace_paths("v1", kind, ident)
        namespaced.append((detail_path, info_path))
    if training_type == 5 and namespaced:
        return [namespaced[0], plan, *namespaced[1:]]
    return [plan, *namespaced]


def namespace_order(training_type: int | None) -> list[str]:
    """Preferred detail namespace, then the ones the other clients fall back to."""
    if training_type == 5:
        return ["ctt", "course", "free"]
    if training_type == 2:
        return ["course", "free", "ctt"]
    return ["free", "course", "ctt"]


def namespace_paths(version: str, kind: str, training_id: str) -> tuple[str, str]:
    """Detail and info paths for one v1 namespace."""
    if version != "v1":
        raise SpeedianceAPIError("Unknown training detail version")
    detail_name, info_name = NAMESPACE_NAMES[kind]
    ident = str(training_id)
    prefix = "/api/app/trainingInfo"
    return (f"{prefix}/{detail_name}/{ident}", f"{prefix}/{info_name}/{ident}")


def primary_training_id(record: dict) -> str:
    """``trainingId`` when it is set, otherwise the first other session id."""
    for key in ("trainingId", *ALTERNATE_ID_KEYS):
        text = _text_id(record.get(key))
        if text and text != "0":
            return text
    raise SpeedianceAPIError("Training record is missing trainingId")


def session_identity_ids(primary: str, record: dict | None = None) -> list[str]:
    """Ids worth asking for detail, primary first, zeros and duplicates dropped."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        text = _text_id(value)
        if not text or text == "0" or text in seen:
            return
        seen.add(text)
        found.append(text)

    add(primary)
    if record:
        for key in ("trainingId", *ALTERNATE_ID_KEYS):
            add(record.get(key))
    if not found:
        found.append(str(primary))
    return found


def _url_path(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path or url
    if parsed.query:
        return f"{path}?{parsed.query}"
    return path


def extract_weight_unit(data: Any) -> str | None:
    """Read weightUnit from a login or userinfo payload.

    The login body carries ``weightUnit``. ``GET /api/app/userinfo`` returns the
    same field, sometimes nested under ``userinfo`` / ``appUserInfo``, and the
    reference clients write the same setting as ``unit`` (0 metric, 1 imperial)
    on that profile object.
    """
    found = _find_key(data, ("weightunit", "weight_unit"), 0)
    if found:
        return found
    return _find_profile_unit(data, 0)


def _find_key(data: Any, keys: tuple[str, ...], depth: int) -> str | None:
    if not isinstance(data, dict) or depth > 6:
        return None
    folded = {str(key).lower(): value for key, value in data.items()}
    for key in keys:
        if folded.get(key) not in (None, ""):
            try:
                return parse_weight_unit(folded.get(key))
            except SpeedianceAPIError:
                continue
    for value in data.values():
        if isinstance(value, dict):
            found = _find_key(value, keys, depth + 1)
            if found:
                return found
    return None


def _find_profile_unit(data: Any, depth: int) -> str | None:
    if not isinstance(data, dict) or depth > 6:
        return None
    folded = {str(key).lower(): value for key, value in data.items()}
    looks_like_profile = depth == 0 or any(hint in folded for hint in _PROFILE_HINTS)
    if looks_like_profile and folded.get("unit") not in (None, ""):
        try:
            return parse_weight_unit(folded.get("unit"))
        except SpeedianceAPIError:
            pass
    for value in data.values():
        if isinstance(value, dict):
            found = _find_profile_unit(value, depth + 1)
            if found:
                return found
    return None


def _muscle_text(payload: dict) -> str | None:
    for key in MUSCLE_NAME_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _group_id(payload: dict) -> str | None:
    for key in GROUP_ID_KEYS:
        found = _text_id(payload.get(key))
        if found:
            return found
    return None


def _text_id(value: Any) -> str | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _library_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _interpret_response(response: requests.Response) -> tuple[str, Any]:
    status = response.status_code
    if status == 401:
        return "auth", "Speediance unauthorized"
    body, raw_text = _response_body(response)
    message = ""
    code = None
    if isinstance(body, dict):
        code = _as_int(body.get("code"))
        message = redact(str(body.get("message") or ""))
    else:
        message = redact(raw_text)
    if code == 91:
        return "auth", message or "Speediance unauthorized"
    if is_rate_limited(status, code, message):
        return "rate_limit", "Speediance rate limit: Request too frequent, please try again later"
    if not isinstance(body, dict):
        return "error", f"Speediance returned non-JSON (HTTP {status})"
    if code not in (None, 0):
        return "error", message or f"Speediance API error code {code}"
    if status >= 400:
        return "error", f"Speediance HTTP {status}"
    return "ok", body.get("data")


def _response_body(response: requests.Response) -> tuple[Any, str]:
    text = ""
    try:
        text = response.text or ""
    except Exception:
        text = ""
    try:
        return response.json(), text
    except ValueError:
        return None, text[:300]
