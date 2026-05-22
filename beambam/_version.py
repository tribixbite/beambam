"""beambam._version — canonical version + User-Agent string.

The version reported by `beambam --version`, the daemon's `/healthz`
banner, the User-Agent on outbound HTTP, the MCP server's
`server.version`, and the bridge JSON-RPC handshake (`bridge_version`)
all need to agree. Centralising the lookup here means a single source
of truth, importable from anywhere without dragging the rest of the
bridge in.

Lookup order:
  1. `importlib.metadata.version("beambam")` — works for both
     installed wheels and editable installs (`pip install -e .`).
  2. A pinned source-checkout fallback for clones where the package
     hasn't been installed at all (someone running `python3
     x2d_bridge.py …` from a fresh `git clone`).

The fallback string carries a `+source` local-version segment so
anyone parsing this can tell the binary is "the source tree at this
commit" rather than a pinned PyPI release. Bump it in lockstep with
`pyproject.toml`'s `[project] version`.
"""
from __future__ import annotations

# Pinned fallback — bump together with pyproject.toml's version field.
# The `+source` local-segment makes it obvious this is a checkout, not
# a pinned release.
_SOURCE_FALLBACK = "1.3.0+source"


def _package_version() -> str:
    """Return the installed `beambam` version, or `_SOURCE_FALLBACK`
    when the package isn't installed (e.g. a fresh clone without
    `pip install -e .`)."""
    try:
        from importlib.metadata import version
        return version("beambam")
    except Exception:                                   # noqa: BLE001
        # importlib.metadata.PackageNotFoundError, broken setup.py,
        # zipimport edge cases — collapse them all to the fallback.
        return _SOURCE_FALLBACK


# Resolved once at import time so the cost of stat-ing the dist-info
# directory is paid exactly once per process. Re-exported from
# x2d_bridge as `PACKAGE_VERSION` for back-compat with existing call
# sites in beambam/cli/info.py and runtime/*.
PACKAGE_VERSION = _package_version()


__all__ = ["PACKAGE_VERSION", "_package_version", "_SOURCE_FALLBACK"]
