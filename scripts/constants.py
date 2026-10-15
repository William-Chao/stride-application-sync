"""Stable identifiers shared by the sync and the daily export."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = "1.3.0"
SOURCE = "speediance"
TIMEZONE = "America/Los_Angeles"
LA = ZoneInfo(TIMEZONE)
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = Path("~/fitness-data").expanduser()
DEFAULT_DAILY_DIR = Path("~/fitness-data/daily").expanduser()
DEFAULT_REQUEST_DELAY_S = 1.5
FIRST_SYNC_MONTHS = 12
RATE_LIMIT_BACKOFF_S = (10, 30, 60)

# International pound, used only to convert a pound-denominated source into kg.
LB_PER_KG = "2.2046226218"
