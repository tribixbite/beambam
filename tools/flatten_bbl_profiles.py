#!/usr/bin/env python3
"""tools/flatten_bbl_profiles.py — generate BambuStudio's flattened *_full
profile dirs from the inheritance-based source profiles.

BambuStudio's CLI loads system presets by name from
`resources/profiles/BBL/{process,filament,machine}_full/` — the
inheritance-RESOLVED (flattened) copies of the `{process,filament,machine}/`
source profiles. The bs-bionic build in this repo ships only the source
(inheritance) profiles, so the slicer aborts with
`can not find setting file: .../process_full/<name>.json` the moment a machine
profile resolves its default process by name. This regenerates the `_full`
dirs by resolving each profile's `inherits` chain (child keys override parent).

Usage:
  python3 tools/flatten_bbl_profiles.py [BBL_PROFILE_DIR]
  # default: bs-bionic/resources/profiles/BBL

Idempotent — safe to re-run after a profiles update.
"""
from __future__ import annotations

import json
import os
import sys


def _flatten_dir(base: str, sub: str) -> int:
    src = os.path.join(base, sub)
    dst = os.path.join(base, sub + "_full")
    if not os.path.isdir(src):
        return 0
    os.makedirs(dst, exist_ok=True)

    profs: dict[str, dict] = {}
    for f in os.listdir(src):
        if f.endswith(".json"):
            try:
                profs[f[:-5]] = json.load(open(os.path.join(src, f)))
            except (OSError, json.JSONDecodeError):
                pass

    def resolve(name: str, stack: tuple) -> dict:
        p = profs.get(name)
        if p is None or name in stack:            # missing / cycle guard
            return {}
        merged = dict(resolve(p["inherits"], stack + (name,))) if p.get("inherits") else {}
        for k, v in p.items():
            if k != "inherits":
                merged[k] = v                     # child overrides parent
        return merged

    n = 0
    for name in profs:
        flat = resolve(name, ())
        flat.pop("inherits", None)
        json.dump(flat, open(os.path.join(dst, name + ".json"), "w"),
                  ensure_ascii=False)
        n += 1
    return n


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "bs-bionic/resources/profiles/BBL"
    if not os.path.isdir(base):
        print(f"profile dir not found: {base}", file=sys.stderr)
        return 1
    total = 0
    for sub in ("process", "filament", "machine"):
        n = _flatten_dir(base, sub)
        print(f"  flattened {n:>5} -> {base}/{sub}_full")
        total += n
    print(f"done: {total} profiles flattened")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
