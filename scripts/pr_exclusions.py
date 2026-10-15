"""Sets that stay in the log but do not count as a PR or an estimated 1RM.

``config/pr_exclusions.json`` lists a Los Angeles date and an exercise name.
``min_weight_kg`` is optional. When it is set, only sets whose kilogram load
is at least that heavy are excluded. When it is omitted, every set of that
exercise on that date is excluded. A missing file excludes nothing. A broken
file raises so the sync cannot quietly keep the set in the records.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from scripts.constants import ROOT

CONFIG_PATH = ROOT / "config" / "pr_exclusions.json"
_RULE_FIELDS = {"date", "exercise", "min_weight_kg"}


class PrExclusionError(ValueError):
    """The exclusion file is present but not usable."""


def load_exclusions(path: Path | None = None) -> list[dict]:
    target = CONFIG_PATH if path is None else path
    if not target.is_file():
        return []
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PrExclusionError(f"PR exclusion file is not valid JSON: {target.name}") from exc
    return parse_exclusions(document)


def parse_exclusions(document: Any) -> list[dict]:
    if not isinstance(document, dict) or not isinstance(document.get("exclusions"), list):
        raise PrExclusionError("PR exclusion file must be an object with an exclusions list")
    return [_parse_rule(raw, index) for index, raw in enumerate(document["exclusions"])]


def matches(rule: dict, day: str, exercise: str, weight_kg: Any) -> bool:
    """True when this set should be left out of PRs and estimated 1RMs."""
    if rule.get("date") != day:
        return False
    if str(rule.get("exercise", "")).strip() != str(exercise).strip():
        return False
    if "min_weight_kg" not in rule:
        return True
    if isinstance(weight_kg, bool) or not isinstance(weight_kg, (int, float)):
        return False
    return Decimal(str(weight_kg)) >= Decimal(str(rule["min_weight_kg"]))


def apply_exclusions(exercises: list[dict], day: str, rules: list[dict]) -> None:
    """Flag matching sets and clear their estimated 1RMs. Other fields stay."""
    if not rules:
        return
    for exercise in exercises:
        name = exercise.get("name")
        if not isinstance(name, str):
            continue
        for item in exercise.get("sets") or []:
            if not isinstance(item, dict):
                continue
            if any(matches(rule, day, name, item.get("weight_kg")) for rule in rules):
                item["excluded_from_prs"] = True
                item["estimated_1rm_kg"] = None
                item["estimated_1rm_lb"] = None


def _parse_rule(raw: Any, index: int) -> dict:
    if not isinstance(raw, dict):
        raise PrExclusionError(f"PR exclusion {index} must be an object")
    unknown = set(raw) - _RULE_FIELDS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise PrExclusionError(f"PR exclusion {index} has unknown fields: {names}")
    date = raw.get("date")
    exercise = raw.get("exercise")
    if not isinstance(date, str) or _invalid_date(date):
        raise PrExclusionError(f"PR exclusion {index} needs a YYYY-MM-DD date")
    if not isinstance(exercise, str) or not exercise.strip():
        raise PrExclusionError(f"PR exclusion {index} needs an exercise name")
    rule: dict[str, Any] = {"date": date, "exercise": exercise.strip()}
    if "min_weight_kg" in raw:
        weight = raw["min_weight_kg"]
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
            raise PrExclusionError(f"PR exclusion {index} min_weight_kg must be a number")
        rule["min_weight_kg"] = weight
    return rule


def _invalid_date(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return True
    return parsed.strftime("%Y-%m-%d") != value
