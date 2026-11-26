#!/usr/bin/env python3
"""tools/flatten_bbl_profiles.py — generate Bambu Studio's flattened *_full profile dirs.

Kept for the scripts that call it (x2d_slice.py, tools/x2d_cli_slice.sh); the code is
`beambam.profile_flatten`, which `beambam slicer install` also uses.

Usage:
  python3 tools/flatten_bbl_profiles.py [BBL_PROFILE_DIR]
  # default: bs-bionic/resources/profiles/BBL
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beambam.profile_flatten import main  # noqa: E402  (after the path tweak)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
