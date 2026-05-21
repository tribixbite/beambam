"""beambam.upgrade — `beambam upgrade` subcommand.

Pip self-upgrade for `uvx beambam` / `pip install beambam` users.

  beambam upgrade --check        # report installed + latest, no changes
  beambam upgrade                # in-place upgrade via `pip install -U beambam`
  beambam upgrade --pre          # include pre-releases

Strategy:
  * `installed_version` via `importlib.metadata.version("beambam")`.
  * `latest_version` via the PyPI JSON API
    `https://pypi.org/pypi/beambam/json` — anonymous, no rate limit issues
    at the < ~10 req/sec we'll ever hit.
  * Upgrade is a `subprocess.run([python, -m, pip, install, --upgrade,
    beambam])` so we inherit the user's pip / venv config.

Special cases:
  * Running from a source checkout (no installed package) → print a
    helpful message; don't attempt to `pip install` over a dev tree.
  * `uvx beambam` invokes a tool-managed venv: pip works there but the
    user should re-run `uvx --refresh-package beambam beambam` for
    persistent upgrade. We detect this via `$VIRTUAL_ENV` containing
    `/uv/tools/`.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request


PYPI_JSON = "https://pypi.org/pypi/beambam/json"


# ----- version discovery --------------------------------------------------


def installed_version() -> str | None:
    """Currently-installed version, or None if running from a source
    checkout (the package isn't installed)."""
    try:
        from importlib.metadata import version
        return version("beambam")
    except Exception:
        return None


def latest_version(*, include_pre: bool = False,
                    timeout: float = 10.0,
                    url: str = PYPI_JSON) -> str:
    """Fetch the highest stable (or pre-release, if include_pre) version
    from PyPI's JSON API.

    Returns a single PEP-440 version string. Raises RuntimeError on
    network failure or malformed response.
    """
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": "beambam-upgrade"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        raise RuntimeError(f"PyPI query failed: {e}") from e
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PyPI returned non-JSON: {raw[:200]}") from e
    if include_pre:
        # info.version is the latest including pre-releases.
        v = body.get("info", {}).get("version")
        if not v:
            raise RuntimeError("PyPI response missing info.version")
        return v
    # Else: pick highest non-prerelease key from `releases` (PyPI's
    # `info.version` is the *latest* release which on a fresh pre-release
    # would include the rc/alpha — filter those out).
    releases = body.get("releases", {})
    if not releases:
        raise RuntimeError("PyPI response missing releases")
    candidates = [v for v in releases.keys()
                  if not _is_prerelease(v) and releases[v]]
    if not candidates:
        # All releases are pre-releases — fall back to info.version.
        return body.get("info", {}).get("version") or ""
    return max(candidates, key=_version_key)


def _is_prerelease(v: str) -> bool:
    """Heuristic: PEP-440 pre-release markers contain a/b/rc, dev, post."""
    s = v.lower()
    return any(m in s for m in ("a", "b", "rc", "dev"))


def _version_key(v: str) -> tuple:
    """Cheap PEP-440 version key for sorting. Splits on '.' and parses
    the numeric prefix of each part. Good enough for X.Y.Z comparisons —
    we never sort pre-releases here (they're filtered out)."""
    parts = []
    for p in v.split("."):
        num = ""
        for c in p:
            if c.isdigit():
                num += c
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


# ----- environment detection ---------------------------------------------


def is_uvx_environment() -> bool:
    """True if we're running under `uvx beambam`. uvx provisions an
    ephemeral venv under `~/.local/share/uv/tools/<pkg>/` — detectable
    by `$VIRTUAL_ENV` containing `uv/tools` or `uv-tool`."""
    venv = os.environ.get("VIRTUAL_ENV", "")
    return "uv/tools" in venv or "uv-tool" in venv


def is_source_checkout() -> bool:
    """True if `beambam` isn't installed at all (running x2d_bridge.py
    directly from a clone). `pip install -U` would create a parallel
    installation that conflicts with `python x2d_bridge.py`."""
    return installed_version() is None


# ----- comparison + upgrade execution -----------------------------------


def compare(installed: str | None, latest: str) -> str:
    """Return one of: 'up-to-date' / 'upgrade-available' / 'dev-ahead'.

    'dev-ahead' = installed version is HIGHER than PyPI latest (common
    on local source checkouts with a bumped version)."""
    if installed is None:
        # Source checkout — no installed package to compare.
        return "source-checkout"
    if installed == latest:
        return "up-to-date"
    if _version_key(installed) > _version_key(latest):
        return "dev-ahead"
    return "upgrade-available"


def run_pip_upgrade(*, pre: bool = False,
                     python: str | None = None) -> int:
    """Invoke `<python> -m pip install --upgrade beambam`. Returns pip's
    exit code. We inherit stdout/stderr so the user sees pip's progress
    in real time.

    `python` defaults to the running interpreter (sys.executable), so
    upgrades land in the same venv that's currently active."""
    cmd = [python or sys.executable, "-m", "pip", "install", "--upgrade"]
    if pre:
        cmd.append("--pre")
    cmd.append("beambam")
    r = subprocess.run(cmd)
    return r.returncode


# ----- CLI wiring --------------------------------------------------------


def cmd_upgrade(args: argparse.Namespace) -> int:
    """Subcommand handler. Reachable as `beambam upgrade [--check] [--pre]`."""
    inst = installed_version()
    try:
        latest = latest_version(include_pre=args.pre)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    inst_str = inst if inst else "(source checkout — not installed)"
    print(f"installed: {inst_str}")
    print(f"latest:    {latest}")

    status = compare(inst, latest)
    if status == "up-to-date":
        print("up to date.")
        return 0
    if status == "dev-ahead":
        print("local version is AHEAD of PyPI — no upgrade needed.")
        return 0
    if status == "source-checkout":
        print(
            "running from a source checkout. To get the latest published "
            "version, run:\n"
            f"    pip install beambam=={latest}\n"
            "or use `uvx beambam` to spawn a clean tool-managed venv.")
        return 0

    # upgrade-available
    if args.check:
        print(f"upgrade available: {inst} → {latest}")
        return 0

    if is_uvx_environment():
        print(
            "detected uvx-managed venv — running pip install -U inside it.\n"
            "Note: for persistent upgrades, also run:\n"
            "    uvx --refresh-package beambam beambam --version")
    print(f"upgrading {inst} → {latest} via "
          f"`{sys.executable} -m pip install --upgrade beambam`…")
    return run_pip_upgrade(pre=args.pre)


def add_subparser(sub: "argparse._SubParsersAction"
                  ) -> argparse.ArgumentParser:
    p = sub.add_parser(
        "upgrade",
        help="Self-upgrade beambam via pip. Use --check for a dry-run "
             "report (installed vs PyPI latest, no install).",
    )
    p.add_argument("--check", action="store_true",
                   help="Report installed + latest versions without "
                        "running pip install. Exit 0 either way.")
    p.add_argument("--pre", action="store_true",
                   help="Include pre-releases (alpha / beta / rc) when "
                        "comparing + when running pip.")
    p.set_defaults(fn=cmd_upgrade)
    return p
