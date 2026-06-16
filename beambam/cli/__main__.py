"""Enable `python -m beambam.cli ...` as an alternative entry point.

Useful for sub-process spawns (e.g. `beambam cam start` forks the
camera proxy) that need to re-invoke the bridge without relying on
console-script binary names being on PATH."""
from __future__ import annotations

import sys

from beambam.cli import main


if __name__ == "__main__":
    sys.exit(main())
