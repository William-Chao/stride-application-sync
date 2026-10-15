"""Pull Speediance history into raw monthly snapshots and normalized JSON.

Reads SPEEDIANCE_USER_ID, SPEEDIANCE_TOKEN, and SPEEDIANCE_REGION from the
environment (or .env). SPEEDIANCE_FAKE=1 replays tests/fixtures instead.

On failure the previous normalized files stay in place, their source_status
becomes stale, and data/status.json records the failure. Tokens are never
written to those files or to the log.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.atomic import atomic_write_json
from scripts.constants import FIRST_SYNC_MONTHS, LA, TIMEZONE
from scripts.envutil import load_dotenv, output_dir
from scripts.normalize_speediance import normalize_bundles, other_muscle_raws
from scripts.redact import public_error
from scripts.speediance_client import SpeedianceAPIError, SpeedianceClient
from scripts.speediance_client import session_local_date
from scripts.timeutil import iso_la, now_la

MONTH_NAME = re.compile(r"^\d{4}-\d{2}$")

logger = logging.getLogger("speediance.sync")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def sync(destination: Path | None = None) -> int:
    load_dotenv()
    root = destination or output_dir()
    stamp = iso_la()
    try:
        client = SpeedianceClient.from_env()
        end = now_la().date()
        start = resolve_start_date(root, end)
        if end < start:
            raise SpeedianceAPIError("SPEEDIANCE_START_DATE is after today")
        if hasattr(client, "refresh_weight_unit"):
            client.refresh_weight_unit()
        logger.info("Speediance sync window %s through %s", start.isoformat(), end.isoformat())
        fresh = client.fetch_between(start, end)
        bundles = _prior_bundles(root, start) + fresh
        history, by_exercise = normalize_bundles(
            bundles,
            generated_at=stamp,
            source_status="ok",
            last_successful_sync=stamp,
        )
        _write_raw_months(root, fresh, getattr(client, "region", "Global"), stamp, start, end)
        _write_normalized(root, history, by_exercise)
        _write_status(root, status="ok", stamp=stamp, last_success=stamp, error=None)
        logger.info("Speediance sync wrote %s sessions", len(history["sessions"]))
        others = other_muscle_raws(history)
        logger.info(
            "Speediance muscle raw strings mapped to other: %s",
            ", ".join(others) if others else "(none)",
        )
        return 0
    except Exception as exc:
        message = public_error(exc)
        logger.error("Speediance sync failed: %s", message)
        _mark_stale(root, stamp, message)
        return 1


def _parse_start(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SpeedianceAPIError("SPEEDIANCE_START_DATE must be YYYY-MM-DD") from exc


def resolve_start_date(root: Path, today: date) -> date:
    """Explicit SPEEDIANCE_START_DATE wins.

    Otherwise the first sync goes back 12 months. Later syncs start at the
    first day of the month before the last successful sync.
    """
    explicit = os.environ.get("SPEEDIANCE_START_DATE", "").strip()
    if explicit:
        return _parse_start(explicit)
    last_month = _last_successful_month(root)
    if last_month is None:
        return _shift_months(today, -FIRST_SYNC_MONTHS)
    year, month = (int(part) for part in last_month.split("-"))
    month -= 1
    if month == 0:
        year -= 1
        month = 12
    return date(year, month, 1)


def _shift_months(day: date, delta: int) -> date:
    index = day.year * 12 + (day.month - 1) + delta
    year, month_index = divmod(index, 12)
    month = month_index + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def _last_successful_month(root: Path) -> str | None:
    status = _read_status(root)
    speediance = (status.get("sources") or {}).get("speediance") or {}
    stamp = speediance.get("last_successful_sync")
    if isinstance(stamp, str) and stamp.strip():
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LA)
            return parsed.astimezone(LA).strftime("%Y-%m")
    monthly = root / "speediance" / "raw" / "monthly"
    if not monthly.is_dir():
        return None
    names = sorted(path.stem for path in monthly.glob("*.json") if MONTH_NAME.match(path.stem))
    return names[-1] if names else None


def _prior_bundles(root: Path, start: date) -> list[dict]:
    """Keep monthly snapshots from before this sync's window."""
    monthly = root / "speediance" / "raw" / "monthly"
    if not monthly.is_dir():
        return []
    cutoff = start.strftime("%Y-%m")
    bundles: list[dict] = []
    for path in sorted(monthly.glob("*.json")):
        if not MONTH_NAME.match(path.stem) or path.stem >= cutoff:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.error("Skipping unreadable monthly snapshot %s", path.name)
            continue
        for record in payload.get("records") or []:
            if isinstance(record, dict) and isinstance(record.get("summary"), dict):
                bundles.append(record)
    return bundles


def _write_raw_months(
    root: Path,
    bundles: list[dict],
    region: str,
    stamp: str,
    start: date,
    end: date,
) -> None:
    monthly = root / "speediance" / "raw" / "monthly"
    grouped: dict[str, list[dict]] = defaultdict(list)
    for bundle in bundles:
        summary = bundle.get("summary") or {}
        session_date = session_local_date(summary)
        if session_date is None:
            continue
        grouped[session_date.strftime("%Y-%m")].append(
            {
                "summary": summary,
                "session": bundle.get("session") or {},
                "detail": bundle.get("detail") or [],
                "detail_available": bool(bundle.get("detail_available")),
                "source_weight_unit": bundle.get("source_weight_unit") or "kg",
            }
        )
    written: set[str] = set()
    for month, records in sorted(grouped.items()):
        payload = {
            "month": month,
            "timezone": TIMEZONE,
            "fetched_at": stamp,
            "region": region,
            "records": records,
        }
        atomic_write_json(monthly / f"{month}.json", payload)
        written.add(month)
    if monthly.is_dir():
        cursor = date(start.year, start.month, 1)
        last = date(end.year, end.month, 1)
        while cursor <= last:
            month = cursor.strftime("%Y-%m")
            path = monthly / f"{month}.json"
            if month not in written and path.is_file():
                path.unlink()
            if cursor.month == 12:
                cursor = date(cursor.year + 1, 1, 1)
            else:
                cursor = date(cursor.year, cursor.month + 1, 1)


def _write_normalized(root: Path, history: dict, by_exercise: dict) -> None:
    folder = root / "speediance" / "normalized"
    atomic_write_json(folder / "history.json", history)
    atomic_write_json(folder / "by_exercise.json", by_exercise)


def _status_path(root: Path) -> Path:
    return root / "status.json"


def _read_status(root: Path) -> dict:
    path = _status_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_status(root: Path, *, status: str, stamp: str, last_success: str | None, error: str | None) -> None:
    from scripts.constants import SCHEMA_VERSION

    payload = {
        "schema_version": SCHEMA_VERSION,
        "timezone": TIMEZONE,
        "generated_at": stamp,
        "sources": {
            "speediance": {
                "status": status,
                "last_successful_sync": last_success,
                "last_attempt": stamp,
                "error": error,
            }
        },
    }
    atomic_write_json(_status_path(root), payload)


def _mark_stale(root: Path, stamp: str, message: str) -> None:
    previous = _read_status(root)
    speediance = (previous.get("sources") or {}).get("speediance") or {}
    last_success = speediance.get("last_successful_sync")
    normalized = root / "speediance" / "normalized"
    for name in ("history.json", "by_exercise.json"):
        _stamp_stale(normalized / name, stamp)
    _write_status(root, status="stale", stamp=stamp, last_success=last_success, error=message)


def _stamp_stale(path: Path, stamp: str) -> None:
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.error("Leaving unreadable normalized file in place: %s", path.name)
        return
    if not isinstance(document, dict):
        return
    document["source_status"] = "stale"
    document["generated_at"] = stamp
    atomic_write_json(path, document)


def main() -> int:
    configure_logging()
    return sync()


if __name__ == "__main__":
    sys.exit(main())
