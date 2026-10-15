"""Record redacted raw responses for one training id.

Uses the same credentials, pace, and rate-limit backoff as a sync. Prints one
JSON document to stdout. Request headers are not included.

    python scripts/debug_training_detail.py 670355 --date 2026-10-13
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.detail_probe import build_report, collect_probes, lookup_windows, probe_targets
from scripts.redact import public_error
from scripts.speediance_client import (
    FakeSpeedianceClient,
    SpeedianceAuthError,
    SpeedianceClient,
    _text_id,
)
from scripts.timeutil import now_la

logger = logging.getLogger("speediance.probe")

_ID_KEYS = (
    "trainingId",
    "id",
    "uuid",
    "trainingInfoId",
    "dataId",
    "recordId",
    "templateId",
    "templateCode",
    "courseId",
    "planId",
)


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)


def find_summary(client: SpeedianceClient, training_id: str, on: date | None) -> dict | None:
    """History row whose id fields include ``training_id``."""
    wanted = str(training_id)
    for start, end in lookup_windows(on, now_la().date()):
        logger.info("Speediance detail probe looking up %s through %s", start.isoformat(), end.isoformat())
        for record in client.get_training_records(start.isoformat(), end.isoformat()):
            if _record_matches(record, wanted):
                return record
    return None


def run(training_id: str, on: date | None) -> tuple[dict, int]:
    client = SpeedianceClient.from_env()
    if isinstance(client, FakeSpeedianceClient):
        raise SpeedianceAuthError("The detail probe needs live credentials. Unset SPEEDIANCE_FAKE.")
    summary = find_summary(client, training_id, on)
    if summary is None:
        logger.info("Speediance detail probe did not find %s in the lookup window", training_id)
    targets = probe_targets(training_id, record=summary, region=getattr(client, "region", "Global"))
    logger.info("Speediance detail probe requesting %s endpoints for %s", len(targets), training_id)
    probes, stopped = collect_probes(client, targets)
    report = build_report(
        training_id,
        lookup_date=on.isoformat() if on else None,
        summary=summary,
        probes=probes,
        stopped=stopped,
    )
    return report, 0 if stopped is None else 1


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Probe Speediance detail endpoints for one training id")
    parser.add_argument("training_id", help="trainingId to probe, for example 670355")
    parser.add_argument("--date", help="Session date YYYY-MM-DD. Defaults to this month and the previous month.")
    args = parser.parse_args(argv)
    on = None
    if args.date:
        try:
            on = date.fromisoformat(args.date)
        except ValueError:
            logger.error("Speediance detail probe --date must be YYYY-MM-DD")
            return 2
    try:
        report, code = run(args.training_id, on)
    except SpeedianceAuthError as exc:
        logger.error("Speediance detail probe failed: %s", public_error(exc))
        return 2
    except Exception as exc:
        logger.error("Speediance detail probe failed: %s", public_error(exc))
        return 1
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


def _record_matches(record: dict, training_id: str) -> bool:
    for key in _ID_KEYS:
        if _text_id(record.get(key)) == training_id:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
