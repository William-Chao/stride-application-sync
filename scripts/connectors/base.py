"""Shared connector contract.

A connector pulls one vendor, writes raw snapshots, then writes normalized
JSON. It does not render a report. Another app implements this same shape:
``source_name`` plus ``sync(output_dir) -> int``.
"""

from __future__ import annotations

from pathlib import Path


class Connector:
    source_name = "vendor"

    def sync(self, output_dir: Path) -> int:
        raise NotImplementedError
