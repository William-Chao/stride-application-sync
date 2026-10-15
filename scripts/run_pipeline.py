"""Run Speediance sync, then the daily export.

Exit codes:
  0  sync and export succeeded
  1  sync failed, export succeeded
  2  export failed
  3  sync and export both failed
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.export_daily import export
from scripts.sync_speediance import configure_logging, sync

logger = logging.getLogger("speediance.pipeline")


def main() -> int:
    configure_logging()
    sync_rc = sync()
    export_rc = export()
    if sync_rc == 0 and export_rc == 0:
        code = 0
    elif sync_rc != 0 and export_rc != 0:
        code = 3
    elif sync_rc != 0:
        code = 1
    else:
        code = 2
    logger.info("pipeline exit %s (sync %s, export %s)", code, sync_rc, export_rc)
    return code


if __name__ == "__main__":
    sys.exit(main())
