"""Write one JSON file per Speediance training day for the report bot.

Reads only data/speediance/normalized/history.json. Rest days produce no file.
SPEEDIANCE_DAILY_DIR defaults to ~/fitness-data/daily. Rewrites are atomic.
A day file that is already current is left byte-identical.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.atomic import write_json_if_changed
from scripts.constants import SCHEMA_VERSION, SOURCE
from scripts.envutil import daily_dir, load_dotenv, output_dir
from scripts.normalize_speediance import _is_loaded, r2
from scripts.redact import public_error
from scripts.timeutil import iso_la

logger = logging.getLogger("speediance.export")
DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def export(destination: Path | None = None, daily: Path | None = None) -> int:
    load_dotenv()
    folder = daily or daily_dir()
    history_path = (destination or output_dir()) / "speediance" / "normalized" / "history.json"
    if not history_path.is_file():
        logger.error("Normalized history is missing; daily export skipped")
        return 2
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
        if not isinstance(history, dict) or not isinstance(history.get("sessions"), list):
            raise ValueError("history.json is missing sessions")
        written = write_daily_files(folder, history["sessions"], generated_at=iso_la())
        logger.info("Daily export kept %s training day files", written)
        return 0
    except Exception as exc:
        logger.error("Daily export failed: %s", public_error(exc))
        return 2


def write_daily_files(folder: Path, sessions: list[dict], *, generated_at: str) -> int:
    by_day: dict[str, list[dict]] = {}
    for session in sessions:
        by_day.setdefault(session["date"], []).append(session)
    for day_sessions in by_day.values():
        day_sessions.sort(key=lambda session: (session["start_time"], session["training_id"]))

    prior: dict[str, dict[str, float]] = {}
    folder.mkdir(parents=True, exist_ok=True)
    for day in sorted(by_day):
        payload, prior = build_day_file(day, by_day[day], prior, generated_at)
        write_json_if_changed(folder / f"{day}.json", payload)

    if by_day:
        _remove_rest_days(folder, set(by_day))
        newest = max(by_day)
        write_json_if_changed(
            folder / "latest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "source": SOURCE,
                "date": newest,
                "file": f"{newest}.json",
                "generated_at": generated_at,
            },
        )
    return len(by_day)


def build_day_file(
    day: str,
    sessions: list[dict],
    prior: dict[str, dict[str, float]],
    generated_at: str,
) -> tuple[dict, dict[str, dict[str, float]]]:
    best = _day_bests(sessions)
    prs = []
    updated = {name: dict(values) for name, values in prior.items()}
    for name in sorted(best):
        current = best[name]
        previous = prior.get(name)
        for metric in ("est_1rm_kg", "top_set_kg"):
            old = None if previous is None else previous[metric]
            new = current[metric]
            if old is None or new > old:
                prs.append({"exercise": name, "metric": metric, "old": old, "new": new})
        slot = updated.setdefault(name, {"est_1rm_kg": current["est_1rm_kg"], "top_set_kg": current["top_set_kg"]})
        slot["est_1rm_kg"] = max(slot["est_1rm_kg"], current["est_1rm_kg"])
        slot["top_set_kg"] = max(slot["top_set_kg"], current["top_set_kg"])
    return (
        {
            "source": SOURCE,
            "version": SCHEMA_VERSION,
            "date": day,
            "generated_at": generated_at,
            "sessions": [_session_export(session) for session in sessions],
            "prs": prs,
        },
        updated,
    )


def _session_export(session: dict) -> dict:
    exercises = []
    derived_groups: list[str] = []
    loaded_sets = []
    for exercise in session.get("exercise_breakdown") or []:
        sets = []
        for item in exercise.get("sets") or []:
            row = _export_set(item)
            if row is None:
                continue
            sets.append(row)
            if _is_loaded(item):
                loaded_sets.append(item)
        muscle = exercise.get("muscle_group") or "other"
        if muscle not in derived_groups:
            derived_groups.append(muscle)
        raw = exercise.get("muscle_group_raw")
        exercises.append(
            {
                "name": exercise["name"],
                "muscle_group": muscle,
                "muscle_group_raw": raw if isinstance(raw, str) and raw.strip() else None,
                "sets": sets,
            }
        )
    groups = session.get("muscle_groups")
    if not isinstance(groups, list):
        groups = derived_groups
    return {
        "start_time": session["start_time"],
        "duration_min": _duration_min(int(session.get("duration_seconds") or 0)),
        "calories": session.get("calories"),
        "calories_estimated": True,
        "total_volume_kg": _volume_kg(loaded_sets),
        "vendor_volume_kg": _optional_kg(session.get("vendor_volume_kg")),
        "muscle_groups": groups,
        "detail_available": bool(session.get("detail_available")),
        "exercises": exercises,
    }


def _export_set(item: dict) -> dict | None:
    reps = item.get("reps")
    duration = item.get("duration_s")
    has_reps = isinstance(reps, int) and not isinstance(reps, bool) and reps > 0
    has_duration = isinstance(duration, int) and not isinstance(duration, bool) and duration > 0
    loaded = _is_loaded(item)
    if not loaded and not has_reps and not has_duration:
        return None
    row: dict = {"weight_kg": item.get("weight_kg") if loaded else None}
    if has_reps:
        row["reps"] = reps
    if has_duration:
        row["duration_s"] = duration
    if "mode" in item:
        row["mode"] = item["mode"]
    if item.get("excluded_from_prs"):
        row["excluded_from_prs"] = True
    return row


def _day_bests(sessions: list[dict]) -> dict[str, dict[str, float]]:
    best: dict[str, dict[str, float]] = {}
    for session in sessions:
        for exercise in session.get("exercise_breakdown") or []:
            name = exercise["name"]
            for item in exercise.get("sets") or []:
                if item.get("excluded_from_prs"):
                    continue
                estimate = item.get("estimated_1rm_kg")
                weight = item.get("weight_kg") or 0
                reps = item.get("reps") or 0
                if not estimate or weight <= 0 or reps <= 0:
                    continue
                slot = best.setdefault(name, {"est_1rm_kg": estimate, "top_set_kg": weight})
                slot["est_1rm_kg"] = max(slot["est_1rm_kg"], estimate)
                slot["top_set_kg"] = max(slot["top_set_kg"], weight)
    return best


def _optional_kg(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return r2(value)


def _volume_kg(sets: list[dict]) -> float:
    total = Decimal("0")
    for item in sets:
        total += Decimal(str(item["weight_kg"])) * Decimal(int(item["reps"]))
    return r2(total)


def _duration_min(seconds: int) -> int | float:
    minutes = Decimal(seconds) / Decimal(60)
    if minutes == minutes.to_integral_value():
        return int(minutes)
    return r2(minutes)


def _remove_rest_days(folder: Path, training_days: set[str]) -> None:
    """Drop Speediance day files that are no longer training days.

    Files without ``source: speediance`` are left alone, including notes the
    report bot may keep beside the export.
    """
    for path in folder.glob("*.json"):
        if path.name == "latest.json" or not DAY_FILE.match(path.name):
            continue
        if path.stem in training_days:
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(document, dict) and document.get("source") == SOURCE:
            path.unlink()


def main() -> int:
    configure_logging()
    return export()


if __name__ == "__main__":
    sys.exit(main())
