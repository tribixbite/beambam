"""beambam.presets — local filament/print preset loader.

Extracted from x2d_bridge.py (Phase 5e batch 3). Used by:

  beambam.cli.daemon.cmd_camera   — calls `_x2d_search_roots()` to
                                    locate the lvl_local module under
                                    dev or dist install layouts.
  ServeServer's _op_user_presets RPC handler — calls
                                    `_load_local_presets()` to merge
                                    Bambu's shipped JSON profiles
                                    with the community-curated set
                                    into the shape PresetBundle's
                                    `load_user_presets` expects.

x2d_bridge.py re-exports each public name. cmd_camera's existing
`from x2d_bridge import _x2d_search_roots` lazy thunk resolves
through that re-export.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _stringify_preset_values(d: dict) -> dict[str, str]:
    """PresetBundle::load_user_presets expects every value as a string
    (or list of strings, which it joins). Re-encode any non-string
    leaf values so the dict round-trips correctly."""
    out: dict[str, str] = {}
    for k, val in d.items():
        if isinstance(val, str):
            out[k] = val
        elif isinstance(val, list):
            out[k] = ",".join(str(x) for x in val)
        elif isinstance(val, bool):
            # bool must be tested before int — bool IS-A int in Python.
            out[k] = str(val).lower()
        elif isinstance(val, (int, float)):
            out[k] = str(val)
        elif val is None:
            out[k] = ""
        else:
            out[k] = json.dumps(val)
    return out


def _x2d_search_roots() -> list[Path]:
    """Candidate roots for shipped data files. Try (in order):
    - the install root (sibling of beambam/ — i.e. repo root in dev
      checkouts, site-packages root in pip installs)
    - the parent (dist tree variants)
    so the same code finds files in either layout.
    """
    from beambam import X2D_ROOT_PATH
    return [X2D_ROOT_PATH, X2D_ROOT_PATH.parent]


def _local_preset_dirs() -> list[Path]:
    """Where to look for shipped BBL filament profiles. The first
    candidate that exists wins; the rest are silently skipped so this
    works in both the dev tree (bs-bionic/...) and the unpacked
    tarball (resources/...)."""
    dirs: list[Path] = []
    for root in _x2d_search_roots():
        dirs.append(
            root / "resources" / "profiles" / "BBL" / "filament")
        dirs.append(
            root / "bs-bionic" / "resources" / "profiles" / "BBL"
            / "filament")
    return dirs


def _load_local_presets() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}

    # Community-curated presets — small JSON shipped under runtime/.
    # Same dev-vs-dist multi-root lookup as the BBL profile dirs.
    community = None
    for root in _x2d_search_roots():
        cand = (root / "runtime" / "network_shim" / "data"
                / "community_filaments.json")
        if cand.exists():
            community = cand
            break
    if community is not None:
        try:
            blob = json.loads(community.read_text())
            for name, raw in blob.items():
                if name.startswith("_"):  # comment keys
                    continue
                if not isinstance(raw, dict):
                    continue
                out[name] = _stringify_preset_values(raw)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[serve] local presets: bad community json: {e}",
                  file=sys.stderr)

    # Vendor-shipped BBL filaments — every "instantiation":"true" entry.
    for d in _local_preset_dirs():
        if not d.is_dir():
            continue
        for jf in sorted(d.glob("*.json")):
            try:
                raw = json.loads(jf.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if raw.get("instantiation") != "true":
                continue
            name = raw.get("name") or jf.stem
            # Already loaded? Community version wins.
            if name in out:
                continue
            out[name] = _stringify_preset_values(raw)
        break  # only the first directory that exists

    return out


__all__ = [
    "_stringify_preset_values",
    "_x2d_search_roots",
    "_local_preset_dirs",
    "_load_local_presets",
]
