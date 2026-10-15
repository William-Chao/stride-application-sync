"""America/Los_Angeles clock. Tests can freeze it with SPEEDIANCE_NOW."""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from scripts.constants import LA


def now_la() -> datetime:
    override = os.environ.get("SPEEDIANCE_NOW", "").strip()
    if not override:
        return datetime.now(LA)
    parsed = datetime.fromisoformat(override)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LA)
    return parsed.astimezone(LA)


def iso_la(moment: datetime | None = None) -> str:
    current = moment or now_la()
    return current.astimezone(LA).isoformat(timespec="seconds")


def utc_offset_hhmm(moment: datetime | None = None) -> str:
    current = (moment or now_la()).astimezone(LA)
    offset = current.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}{minutes:02d}"
