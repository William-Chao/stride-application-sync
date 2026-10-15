"""Turn Speediance history bundles into stable normalized documents.

Weights are stored in kilograms and pounds. When the source account is in
pounds, each load is converted to kilograms before the Epley estimate:

    estimated_1rm = weight_kg * (1 + reps / 30)

Bodyweight and timer sets stay on the session breakdown with ``weight_kg``
null and, when the vendor sent one, ``reps`` and/or ``duration_s``. They are
left out of volume, estimated 1RMs, and by_exercise. Loaded sets still require
a positive weight and rep count. Sets listed in ``config/pr_exclusions.json``
stay on the breakdown and still count toward volume, but their estimated 1RM
is cleared and they are left out of by_exercise.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from scripts.constants import LA, LB_PER_KG, SCHEMA_VERSION, SOURCE, TIMEZONE
from scripts.pr_exclusions import apply_exclusions, load_exclusions
from scripts.speediance_client import exercise_set_rows, finished_rep_count, session_start

MUSCLE_KEYS = (
    "mainMuscleGroupName",
    "trainingPartName",
    "muscleGroupName",
    "muscleGroup",
    "muscle_group",
    "trainingPart",
    "partName",
)

CANONICAL_MUSCLES = (
    "chest",
    "back",
    "shoulders",
    "biceps",
    "triceps",
    "legs",
    "glutes",
    "core",
    "full_body",
    "other",
)

# Longer aliases first so "full body" wins over "body" and "biceps" is not a back hit.
_MUSCLE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("full_body", ("full body", "fullbody", "whole body", "total body", "全身")),
    ("biceps", ("biceps", "bicep", "二头")),
    ("triceps", ("triceps", "tricep", "三头")),
    ("glutes", ("glutes", "glute", "臀")),
    ("chest", ("pectoral", "pecs", "chest", "pec", "胸")),
    ("shoulders", ("shoulder", "deltoid", "delts", "delt", "肩")),
    ("back", ("latissimus", "lats", "trapezius", "traps", "rhomboid", "erector", "back", "背")),
    ("legs", ("hamstring", "quad", "adductor", "abductor", "calves", "calf", "thigh", "legs", "leg", "腿")),
    ("core", ("abdominal", "oblique", "core", "abs", "腹", "核心")),
)
NAME_KEYS = ("actionLibraryName", "actionName", "name", "title")
TEMPLATE_KEYS = ("templateName", "courseName", "templateTitle")


def r2(value: Decimal | float | int | str) -> float:
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def lb_to_kg(pounds: float) -> float:
    return r2(Decimal(str(pounds)) / Decimal(LB_PER_KG))


def kg_to_lb(kilograms: float) -> float:
    return r2(Decimal(str(kilograms)) * Decimal(LB_PER_KG))


def epley_kg(weight_kg: float, reps: int) -> float | None:
    if reps <= 0 or weight_kg <= 0:
        return None
    estimate = Decimal(str(weight_kg)) * (Decimal(1) + Decimal(reps) / Decimal(30))
    return r2(estimate)


def normalize_bundles(
    bundles: list[dict],
    *,
    generated_at: str,
    source_status: str,
    last_successful_sync: str | None,
    exclusions: list[dict] | None = None,
) -> tuple[dict, dict]:
    rules = load_exclusions() if exclusions is None else exclusions
    sessions = [_normalize_session(bundle, rules) for bundle in bundles]
    sessions = [session for session in sessions if session is not None]
    sessions.sort(key=lambda session: (session["start_time"], session["training_id"]))
    history = _envelope(generated_at, source_status, last_successful_sync)
    history["sessions"] = sessions
    by_exercise = _envelope(generated_at, source_status, last_successful_sync)
    by_exercise["movements"] = _movements(sessions)
    return history, by_exercise


def _envelope(generated_at: str, source_status: str, last_successful_sync: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "timezone": TIMEZONE,
        "generated_at": generated_at,
        "source_status": source_status,
        "last_successful_sync": last_successful_sync,
    }


def _normalize_session(bundle: dict, rules: list[dict] | None = None) -> dict | None:
    summary = bundle.get("summary") or {}
    start = session_start(summary)
    if start is None:
        return None
    session_info = bundle.get("session") or {}
    fallback_name = _text(summary.get("title") or session_info.get("title")) or "Exercise"
    exercises = []
    for exercise in bundle.get("detail") or []:
        if isinstance(exercise, dict):
            normalized = _normalize_exercise(exercise, bundle.get("source_weight_unit") or "kg", fallback_name)
            if normalized is not None:
                exercises.append(normalized)
    day = start.astimezone(LA).date().isoformat()
    apply_exclusions(exercises, day, rules or [])
    loaded_sets = [item for exercise in exercises for item in exercise["sets"] if _is_loaded(item)]
    total_kg = _sum_volume_kg(loaded_sets)
    template_name = _first_text(summary, TEMPLATE_KEYS) or _first_text(session_info, TEMPLATE_KEYS)
    if "detail_available" in bundle:
        detail_available = bool(bundle.get("detail_available"))
    else:
        detail_available = bool(bundle.get("detail"))
    return {
        "training_id": str(summary.get("trainingId")),
        "date": day,
        "start_time": start.isoformat(timespec="seconds"),
        "title": _text(summary.get("title") or session_info.get("title")) or "Workout",
        "duration_seconds": _as_int(summary.get("trainingTime"), session_info.get("trainingTime")),
        "calories": _calories(summary, session_info),
        "total_volume": {"kg": total_kg, "lb": kg_to_lb(total_kg)},
        "vendor_volume_kg": _vendor_volume_kg(summary, session_info, bundle.get("source_weight_unit") or "kg"),
        "exercise_count": len(exercises),
        "template_name": template_name,
        "detail_available": detail_available,
        "muscle_groups": _session_muscles(exercises, bundle.get("muscle_load_names")),
        "exercise_breakdown": exercises,
    }


def _normalize_exercise(exercise: dict, unit: str, fallback_name: str) -> dict | None:
    name = _first_text(exercise, NAME_KEYS)
    raw_sets = exercise_set_rows(exercise)
    if not name:
        name = fallback_name if raw_sets else None
    if not name:
        return None
    is_timer = exercise.get("completionMethod") in (0, "0")
    sets = []
    for raw_set in raw_sets:
        sets.extend(_logical_sets(raw_set, unit, is_timer))
    raw_muscle = _muscle_raw(exercise)
    return {
        "name": name,
        "muscle_group": map_muscle(raw_muscle),
        "muscle_group_raw": raw_muscle,
        "sets": sets,
    }


def _logical_sets(raw_set: dict, unit: str, is_timer: bool) -> list[dict]:
    raw_set = _with_finished_load(raw_set)
    # A plan row carries the prescribed target beside the reps that were done.
    # finishedCount 0 means the set was not performed, even when targetCount
    # and a weight trace for the prescription are present. A timer set keeps
    # its duration.
    if "finishedCount" in raw_set and finished_rep_count(raw_set) <= 0 and not _duration_s(raw_set, is_timer):
        if _as_int(raw_set.get("targetCount")) > 0 or raw_set.get("targetWeight") not in (None, ""):
            return []
    side = {1: "left", 2: "right"}.get(_as_int(raw_set.get("leftRight")))
    mode = raw_set.get("mode", raw_set.get("sportMode"))
    weights = _weight_array(raw_set)
    finished = _as_int(raw_set.get("finishedCount"))
    duration = _duration_s(raw_set, is_timer)
    explicit_zero = "finishedCount" in raw_set and finished <= 0
    segments: list[tuple[int, float | None]]
    if explicit_zero and not duration:
        return []
    if weights and not explicit_zero and (finished == 0 or finished == len(weights)):
        segments = []
        for weight in weights:
            if segments and segments[-1][1] == weight:
                reps, current = segments[-1]
                segments[-1] = (reps + 1, current)
            else:
                segments.append((1, weight))
    else:
        weight = None
        if weights:
            unique = set(weights)
            weight = weights[0] if len(unique) == 1 else max(weights)
        elif finished > 0 and _as_float(raw_set.get("capacity")) > 0:
            weight = _as_float(raw_set.get("capacity")) / finished
        elif raw_set.get("weight") not in (None, ""):
            parsed = _as_float(raw_set.get("weight"))
            weight = parsed if parsed > 0 else None
        segments = [(finished, weight)]

    logical = []
    for index, (reps, weight) in enumerate(segments):
        segment_duration = duration if index == 0 else None
        record = _set_record(reps, weight, unit, side, mode, segment_duration)
        if record is not None:
            logical.append(record)
    return logical


def _set_record(
    reps: int,
    weight: float | None,
    unit: str,
    side: str | None,
    mode: Any,
    duration_s: int | None,
) -> dict | None:
    has_reps = reps > 0
    has_duration = duration_s is not None and duration_s > 0
    if weight is None or weight <= 0 or not has_reps:
        if not has_reps and not has_duration:
            return None
        record = {
            "reps": reps if has_reps else None,
            "side": side,
            "weight_kg": None,
            "weight_lb": None,
            "estimated_1rm_kg": None,
            "estimated_1rm_lb": None,
        }
    else:
        if unit == "lb":
            weight_lb = r2(weight)
            weight_kg = lb_to_kg(weight_lb)
        else:
            weight_kg = r2(weight)
            weight_lb = kg_to_lb(weight_kg)
        estimate_kg = epley_kg(weight_kg, reps)
        record = {
            "reps": reps,
            "side": side,
            "weight_kg": weight_kg,
            "weight_lb": weight_lb,
            "estimated_1rm_kg": estimate_kg,
            "estimated_1rm_lb": None if estimate_kg is None else kg_to_lb(estimate_kg),
        }
    if has_duration:
        record["duration_s"] = duration_s
    if mode not in (None, ""):
        record["mode"] = mode if isinstance(mode, (int, float, str)) else str(mode)
    return record


def _weight_array(raw_set: dict) -> list[float]:
    detail = raw_set.get("trainingInfoDetail") or {}
    if not isinstance(detail, dict):
        return []
    side = _as_int(raw_set.get("leftRight"))
    if side == 1 and isinstance(detail.get("leftWeights"), list):
        raw = detail["leftWeights"]
    elif side == 2 and isinstance(detail.get("rightWeights"), list):
        raw = detail["rightWeights"]
    elif isinstance(detail.get("weights"), list):
        raw = detail["weights"]
    else:
        return []
    values = []
    for item in raw:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            continue
    return values


def _movements(sessions: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for session in sessions:
        for exercise in session["exercise_breakdown"]:
            for item in exercise["sets"]:
                if item.get("excluded_from_prs") or not _is_loaded(item) or item["estimated_1rm_kg"] is None:
                    continue
                grouped.setdefault(exercise["name"], []).append(
                    {
                        "weight": {"kg": item["weight_kg"], "lb": item["weight_lb"]},
                        "reps": item["reps"],
                        "side": item["side"],
                        "session_id": session["training_id"],
                        "timestamp": session["start_time"],
                        "estimated_1rm": {
                            "kg": item["estimated_1rm_kg"],
                            "lb": item["estimated_1rm_lb"],
                        },
                    }
                )
    movements = []
    for name in sorted(grouped):
        sets = sorted(grouped[name], key=lambda item: (item["timestamp"], item["session_id"], item["side"] or ""))
        movements.append({"name": name, "sets": sets})
    return movements


def _vendor_volume_kg(summary: dict, session_info: dict, unit: str) -> float | None:
    """Speediance's session totalCapacity, in kilograms. Reference only."""
    raw = None
    for source in (summary, session_info):
        if not isinstance(source, dict) or "totalCapacity" not in source:
            continue
        candidate = source.get("totalCapacity")
        if isinstance(candidate, bool) or candidate in (None, ""):
            continue
        raw = candidate
        break
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if unit == "lb":
        return lb_to_kg(value)
    return r2(value)


def _sum_volume_kg(sets: list[dict]) -> float:
    total = Decimal("0")
    for item in sets:
        total += Decimal(str(item["weight_kg"])) * Decimal(int(item["reps"]))
    return r2(total)


def map_muscle(raw: str | None) -> str:
    """Map a vendor muscle or body-part name onto the fixed consumer set."""
    if not raw:
        return "other"
    text = " ".join(raw.lower().replace("_", " ").replace("-", " ").replace("/", " ").split())
    for canonical, aliases in _MUSCLE_RULES:
        if any(alias in text for alias in aliases):
            return canonical
    return "other"


def other_muscle_raws(history: dict) -> list[str]:
    """Sorted unique vendor muscle strings that landed on ``other``.

    Null and blank raw values are omitted. Names that mapped onto a real
    muscle group are omitted, so the list is the one to extend the alias table.
    """
    found: set[str] = set()
    for session in history.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        for exercise in session.get("exercise_breakdown") or []:
            if not isinstance(exercise, dict) or exercise.get("muscle_group") != "other":
                continue
            raw = exercise.get("muscle_group_raw")
            if isinstance(raw, str) and raw.strip():
                found.add(raw.strip())
    return sorted(found)


def _muscle_raw(exercise: dict) -> str | None:
    for key in MUSCLE_KEYS:
        text = _text(exercise.get(key))
        if text:
            return text
    return None


def _session_muscles(exercises: list[dict], fallback: Any = None) -> list[str]:
    groups: list[str] = []
    named = False
    for exercise in exercises:
        if exercise.get("muscle_group_raw"):
            named = True
        muscle = exercise.get("muscle_group") or "other"
        if muscle not in groups:
            groups.append(muscle)
    if named or not isinstance(fallback, list):
        return groups
    loaded: list[str] = []
    for raw in fallback:
        if not isinstance(raw, str) or not raw.strip():
            continue
        muscle = map_muscle(raw.strip())
        if muscle not in loaded:
            loaded.append(muscle)
    return loaded or groups


def _with_finished_load(raw_set: dict) -> dict:
    """Copy a finished load onto ``weight`` without reading target fields."""
    out = raw_set
    if "finishedCount" not in out:
        nested = out.get("finishedReps")
        if isinstance(nested, (int, float)) and not isinstance(nested, bool):
            out = dict(out)
            out["finishedCount"] = int(nested)
    if out.get("weight") not in (None, "") or _weight_trace(out):
        return out
    for key in ("finishedWeight", "actualWeight", "completeWeight"):
        if out.get(key) not in (None, ""):
            if out is raw_set:
                out = dict(raw_set)
            out["weight"] = out[key]
            break
    return out


def _weight_trace(raw_set: dict) -> bool:
    detail = raw_set.get("trainingInfoDetail") or {}
    if not isinstance(detail, dict):
        return False
    return any(isinstance(detail.get(key), list) and detail.get(key) for key in ("weights", "leftWeights", "rightWeights"))


def _is_loaded(item: dict) -> bool:
    weight = item.get("weight_kg")
    reps = item.get("reps")
    if isinstance(weight, bool) or isinstance(reps, bool):
        return False
    return isinstance(weight, (int, float)) and weight > 0 and isinstance(reps, int) and reps > 0


def _duration_s(raw_set: dict, is_timer: bool) -> int | None:
    for key in ("time", "duration", "trainTime"):
        value = _positive_int(raw_set.get(key))
        if value:
            return value
    detail = raw_set.get("trainingInfoDetail") or {}
    if isinstance(detail, dict):
        for key in ("time", "duration"):
            value = _positive_int(detail.get(key))
            if value:
                return value
    if is_timer:
        return _positive_int(raw_set.get("targetCount"))
    return None


def _positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _first_text(payload: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = _text(payload.get(key))
        if text:
            return text
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _calories(summary: dict, session_info: dict) -> float | None:
    for source in (summary, session_info):
        raw = source.get("calorie", source.get("calories"))
        if raw not in (None, ""):
            try:
                return r2(raw)
            except Exception:
                continue
    return None


def _as_int(*values: Any) -> int:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return int(float(value))
        except (TypeError, ValueError):
            continue
    return 0


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

