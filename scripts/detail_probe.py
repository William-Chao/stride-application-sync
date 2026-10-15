"""Candidate detail URLs and redacted probe reports.

The production sync calls planTrainingInfoDetail and the v1 trainingInfo
namespaces. This module lists those paths, plus the muscle-load totals
endpoint, for one training id. Nothing here opens a socket.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

from scripts.redact import public_error, redact, redact_value
from scripts.speediance_client import (
    DEFAULT_VERSIONCODE,
    REGION_HOSTS,
    SpeedianceAPIError,
    SpeedianceAuthError,
    SpeedianceRateLimitError,
    detail_candidates,
    namespace_order,
    namespace_paths,
    normalize_region,
    session_identity_ids,
)
from scripts.speediance_client import _as_int, _text_id

logger = logging.getLogger("speediance.probe")

BODY_LIMIT = 200_000
TEMPLATE_ID_KEYS = ("templateId", "templateCode", "courseId", "planId")
_LIST_KEYS = (
    "actionLibraryList",
    "cttActionLibraryTrainingInfoList",
    "trainingInfoList",
    "finishedActionList",
    "list",
    "records",
    "detail",
    "data",
)
SUMMARY_KEYS = (
    "type",
    "courseType",
    "courseTypeStr",
    "title",
    "name",
    "trainingId",
    "id",
    "uuid",
    "templateId",
    "templateCode",
    "courseId",
    "planId",
    "trainingInfoId",
    "dataId",
    "recordId",
    "totalCapacity",
    "startTimestamp",
    "startTime",
    "trainingTime",
)


def probe_targets(training_id: str, *, record: dict | None, region: str) -> list[dict]:
    """Production detail order for one session, then muscle-load totals.

    Plan detail is requested for the training id only. v2, v3, Versioncode
    42200, and api3 are omitted: those calls 404, reject the nonce, or reject
    a token that is valid on api2.
    """
    host = REGION_HOSTS[normalize_region(region).lower()]
    record = record or {}
    training_type = _as_int(record.get("type"))
    order = namespace_order(training_type)
    session_ids = session_identity_ids(str(training_id), record)
    template_ids = _template_ids(record, session_ids)
    primary = session_ids[0]
    targets: list[dict] = []

    def add(label: str, path: str) -> None:
        targets.append(
            {
                "label": label,
                "path": path,
                "host": host,
                "versioncode": DEFAULT_VERSIONCODE,
                "speculative": False,
            }
        )

    for ident in session_ids:
        for detail_path, info_path in detail_candidates(training_type, ident):
            if ident != primary and "planTrainingInfoDetail" in detail_path:
                continue
            add(detail_path, detail_path)
            if info_path:
                add(info_path, info_path)
    for ident in template_ids:
        for kind in order:
            detail_path, info_path = namespace_paths("v1", kind, ident)
            add(detail_path, detail_path)
            add(info_path, info_path)
    ident = quote(primary, safe="")
    add(
        f"muscleLoad {primary}",
        f"/api/mobile/muscleLoad/detail?trainingId={ident}",
    )
    return targets


def lookup_windows(on: date | None, today: date) -> list[tuple[date, date]]:
    """Month of ``on``, or this month and the previous month."""
    days = [on] if on is not None else [today, _previous_month(today)]
    windows: list[tuple[date, date]] = []
    for day in days:
        start, end = _month_window(day)
        if start > today:
            continue
        if end > today:
            end = today
        windows.append((start, end))
    return windows


def summary_view(record: dict | None) -> dict | None:
    if not isinstance(record, dict):
        return None
    picked = {key: record.get(key) for key in SUMMARY_KEYS if key in record}
    return redact_value(picked)


def format_capture(target: dict, captured: dict) -> dict:
    """One probe row. Headers are not copied. Bodies are redacted and size-capped."""
    body = redact_value(captured.get("body"))
    truncated = False
    try:
        encoded = json.dumps(body, ensure_ascii=False)
    except TypeError:
        encoded = redact(str(body))
        body = encoded
    if len(encoded) > BODY_LIMIT:
        truncated = True
        body = encoded[:BODY_LIMIT]
    return {
        "label": target["label"],
        "path": target["path"],
        "host": target["host"],
        "versioncode": target["versioncode"],
        "http_status": captured.get("http_status"),
        "code": captured.get("code"),
        "message": redact(str(captured.get("message") or "")),
        "shape": shape_of(captured.get("data")),
        "truncated": truncated,
        "body": body,
    }


def format_error(target: dict, exc: BaseException, *, kind: str) -> dict:
    return {
        "label": target["label"],
        "path": target["path"],
        "host": target["host"],
        "versioncode": target["versioncode"],
        "http_status": None,
        "code": None,
        "message": public_error(exc),
        "shape": {},
        "truncated": False,
        "body": None,
        "error": kind,
    }


def shape_of(data: Any) -> dict:
    info: dict[str, Any] = {"kind": _kind(data)}
    if isinstance(data, list):
        info["length"] = len(data)
        if data and isinstance(data[0], dict):
            info["item_keys"] = sorted(str(key) for key in data[0])[:40]
    elif isinstance(data, dict):
        info["keys"] = sorted(str(key) for key in data)[:40]
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                info["list_key"] = key
                info["list_length"] = len(value)
                break
    info["has_finishedReps"] = _contains(data, "finishedReps", 0)
    info["has_actionLibraryName"] = _contains(data, "actionLibraryName", 0)
    return info


def collect_probes(client: Any, targets: list[dict]) -> tuple[list[dict], str | None]:
    """Call ``client.capture`` for each target.

    An auth error on the account host stops the run. An auth error on any
    other host is a miss for that probe: the token can be valid on api2 and
    rejected by api3.
    """
    account_host = urlsplit(getattr(client, "base_url", "") or "").hostname or ""
    probes: list[dict] = []
    total = len(targets)
    for index, target in enumerate(targets, start=1):
        logger.info("Speediance detail probe %s/%s %s", index, total, target["path"])
        url = f"https://{target['host']}{target['path']}"
        try:
            captured = client.capture(url, versioncode=target["versioncode"])
        except SpeedianceAuthError as exc:
            foreign_host = bool(account_host) and target.get("host") not in (None, "", account_host)
            if foreign_host:
                probes.append(format_error(target, exc, kind="miss"))
                continue
            probes.append(format_error(target, exc, kind="auth"))
            return probes, "auth"
        except SpeedianceRateLimitError as exc:
            probes.append(format_error(target, exc, kind="rate_limit"))
            return probes, "rate_limit"
        except SpeedianceAPIError as exc:
            probes.append(format_error(target, exc, kind="request"))
            continue
        probes.append(format_capture(target, captured))
    return probes, None


def build_report(
    training_id: str,
    *,
    lookup_date: str | None,
    summary: dict | None,
    probes: list[dict],
    stopped: str | None,
) -> dict:
    return {
        "training_id": str(training_id),
        "lookup_date": lookup_date,
        "summary_found": summary is not None,
        "summary": summary_view(summary),
        "stopped": stopped,
        "probes": probes,
    }


def _template_ids(record: dict, already: list[str]) -> list[str]:
    found: list[str] = []
    seen = set(already)
    for key in TEMPLATE_ID_KEYS:
        text = _text_id(record.get(key))
        if text and text != "0" and text not in seen:
            seen.add(text)
            found.append(text)
    return found


def _kind(data: Any) -> str:
    if data is None:
        return "null"
    if isinstance(data, list):
        return "list"
    if isinstance(data, dict):
        return "object"
    return type(data).__name__


def _contains(data: Any, key: str, depth: int) -> bool:
    if depth > 6:
        return False
    if isinstance(data, dict):
        if key in data:
            return True
        return any(_contains(value, key, depth + 1) for value in data.values())
    if isinstance(data, list):
        return any(_contains(value, key, depth + 1) for value in data[:20])
    return False


def _month_window(day: date) -> tuple[date, date]:
    start = date(day.year, day.month, 1)
    if day.month == 12:
        nxt = date(day.year + 1, 1, 1)
    else:
        nxt = date(day.year, day.month + 1, 1)
    end = nxt.fromordinal(nxt.toordinal() - 1)
    return start, end


def _previous_month(day: date) -> date:
    if day.month == 1:
        return date(day.year - 1, 12, 1)
    return date(day.year, day.month - 1, 1)
