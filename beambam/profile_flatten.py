"""beambam.profile_flatten — Bambu Studio's flattened `*_full` profile folders.

The Bambu Studio CLI loads system presets by name from `resources/profiles/BBL/{process,filament,machine}_full/`:
the inheritance-resolved copies of the `{process,filament,machine}/` source profiles (BambuStudio.cpp, e.g.
`machine_full/<printer_model>.json`). Bambu's build makes them; a tree without them (the phone's source build, a
copy whose build skipped them) slices until a machine profile names its default process, then aborts with
`can not find setting file: .../process_full/<name>.json`. :func:`flatten` writes them by resolving each
profile's `inherits` chain (child keys over parent). Idempotent.

    python -m beambam.profile_flatten [BBL_PROFILE_DIR]     # also tools/flatten_bbl_profiles.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

KINDS = ("process", "filament", "machine")
# Bump when the flattening logic changes so existing (stale) `*_full` folders are rebuilt, not just missing
# ones. v2: resolve `include` templates (a machine's start / end / change-filament G-code) — without it every
# X2D slice got a bare start G-code with no AMS load (M620) and the print came out as air.
FLATTEN_VERSION = 2


def _stamp(profiles: Path) -> Path:
    return profiles / ".flatten_version"


def _stamped_current(profiles: Path) -> bool:
    try:
        return _stamp(profiles).read_text(encoding="utf-8").strip() == str(FLATTEN_VERSION)
    except OSError:
        return False


def missing(profiles: Path) -> list[str]:
    """The `*_full` folders to (re)build: any that `profiles` lacks a copy of, and — when the flattener has
    changed since they were written (`FLATTEN_VERSION`) — every kind it has sources for, so a stale build is
    regenerated rather than silently kept."""
    kinds = [k for k in KINDS if (profiles / k).is_dir()]
    if not _stamped_current(profiles):
        return [f"{k}_full" for k in kinds]
    return [f"{k}_full" for k in kinds if not (profiles / f"{k}_full").is_dir()]


def _flatten_kind(profiles: Path, kind: str) -> int:
    src, dst = profiles / kind, profiles / f"{kind}_full"
    if not src.is_dir():
        return 0
    found: dict[str, dict[str, Any]] = {}
    for f in src.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found[f.stem] = data

    def resolve(name: str, stack: tuple[str, ...]) -> dict[str, Any]:
        p = found.get(name)
        if p is None or name in stack:            # missing parent / inheritance cycle
            return {}
        merged = dict(resolve(str(p["inherits"]), stack + (name,))) if p.get("inherits") else {}
        # `include` pulls in template profiles (Bambu splits a machine's start/end/layer/change_filament
        # G-code into "<name> template <section>" files the profile includes). They apply over the parent and
        # under the profile's own keys. Missing this left every X2D slice with a bare start G-code and no AMS
        # load (M620), so the nozzle was never fed and the print came out as air.
        inc = p.get("include")
        if isinstance(inc, list):
            for name2 in inc:
                merged.update(resolve(str(name2), stack + (name,)))
        merged.update({k: v for k, v in p.items() if k not in ("inherits", "include")})
        return merged

    # Written beside it and renamed into place: `missing` tests the folder, so a run cut short must not leave one.
    work = profiles / f".{kind}_full.tmp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    for name in found:
        flat = resolve(name, ())
        flat.pop("inherits", None)
        flat.pop("include", None)
        (work / f"{name}.json").write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(dst, ignore_errors=True)
    work.replace(dst)
    return len(found)


def flatten(profiles: Path) -> dict[str, int]:
    """Write every `*_full` folder of `profiles`; the number of profiles flattened per kind."""
    counts = {kind: _flatten_kind(profiles, kind) for kind in KINDS}
    try:
        _stamp(profiles).write_text(str(FLATTEN_VERSION), encoding="utf-8")   # mark these as current
    except OSError:
        pass
    return counts


def main(argv: list[str]) -> int:
    base = Path(argv[1] if len(argv) > 1 else "bs-bionic/resources/profiles/BBL")
    if not base.is_dir():
        print(f"profile dir not found: {base}", file=sys.stderr)
        return 1
    counts = flatten(base)
    for kind, n in counts.items():
        print(f"  flattened {n:>5} -> {base / (kind + '_full')}")
    print(f"done: {sum(counts.values())} profiles flattened")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
