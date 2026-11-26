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
import sys
from pathlib import Path
from typing import Any

KINDS = ("process", "filament", "machine")


def missing(profiles: Path) -> list[str]:
    """The `*_full` folders `profiles` (a `resources/profiles/BBL` folder) lacks among those it has sources for."""
    return [f"{k}_full" for k in KINDS if (profiles / k).is_dir() and not (profiles / f"{k}_full").is_dir()]


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
        merged.update({k: v for k, v in p.items() if k != "inherits"})
        return merged

    dst.mkdir(exist_ok=True)
    for name in found:
        flat = resolve(name, ())
        flat.pop("inherits", None)
        (dst / f"{name}.json").write_text(json.dumps(flat, ensure_ascii=False), encoding="utf-8")
    return len(found)


def flatten(profiles: Path) -> dict[str, int]:
    """Write every `*_full` folder of `profiles`; the number of profiles flattened per kind."""
    return {kind: _flatten_kind(profiles, kind) for kind in KINDS}


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
