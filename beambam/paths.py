"""beambam.paths — the one place that decides where beambam keeps its state.

Everything beambam persists per installation (printer credentials, the
recovered signing key + cert id, the printer's device cert, the cloud
session, the print queue, timelapses, camera pid/log) lives in one
directory, the *config dir*:

    $BEAMBAM_HOME            (canonical override)
    $X2D_HOME                (legacy name, still honoured)
    ~/.x2d                   (default — kept for compatibility with every
                              existing install and with Bambu tooling notes)

Credentials file: ``$BEAMBAM_CREDS`` / ``$X2D_CREDS`` or
``<config dir>/credentials`` (INI, see :mod:`beambam.config`).

``.env`` support
----------------
:func:`load_env_files` reads ``KEY=VALUE`` files and copies **only**
beambam's own keys (``X2D_*`` and ``BEAMBAM_*``) into ``os.environ``,
never overriding a variable that is already set. Files read, first hit
wins per key:

1. ``$BEAMBAM_ENV_FILE`` (explicit path), if set
2. ``./.env`` in the current working directory
3. ``<config dir>/.env``

So a printer can be configured with no INI at all::

    # ~/.x2d/.env   (or a project-local .env, git-ignored)
    X2D_IP=192.168.1.42
    X2D_CODE=12345678
    X2D_SERIAL=0309DA000000000

The prefix filter means an unrelated project's ``.env`` in the current
directory cannot leak its secrets (API keys etc.) into beambam's process.
:mod:`beambam` calls :func:`load_env_files` once at import, so every entry
point (CLI, MCP server, daemon, library use) sees the same settings.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "ENV_PREFIXES",
    "config_dir",
    "config_path",
    "credentials_path",
    "load_env_files",
    "parse_env_text",
    "serial_dir",
    "signing_path",
    "signing_write_path",
]

# Only these prefixes are imported from .env files (see module docstring).
ENV_PREFIXES: tuple[str, ...] = ("X2D_", "BEAMBAM_")


def config_dir() -> Path:
    """The per-installation state directory (not created here)."""
    raw = os.environ.get("BEAMBAM_HOME") or os.environ.get("X2D_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".x2d"


def config_path(*parts: str) -> Path:
    """A path inside :func:`config_dir`, e.g. ``config_path("queue.json")``."""
    return config_dir().joinpath(*parts)


def _sanitize_serial(serial: str | None) -> str:
    """A filesystem-safe form of a printer serial for use as a subdir name."""
    import re
    return re.sub(r"[^A-Za-z0-9_-]", "_", (serial or "").strip())


def serial_dir(serial: str | None) -> Path:
    """The per-printer state subdirectory ``<config dir>/<serial>`` (not
    created here). Empty/None serial falls back to the config dir itself."""
    s = _sanitize_serial(serial)
    return config_path(s) if s else config_dir()


def signing_path(name: str, serial: str | None = None) -> Path:
    """Resolve a per-printer signing file (``printer_sign_key.pem``,
    ``printer_cert_id.txt``, ``printer_device_cert.pem``, ``printer_app_cert.pem``).

    Keying these by serial is what makes >1 printer safe: printer B must not
    be handed printer A's device cert / signing key / cert id (F-03). We look
    for ``<config dir>/<serial>/<name>`` first and fall back to the historical
    global ``<config dir>/<name>`` when the per-serial file is absent, so every
    existing single-printer install keeps working untouched."""
    s = _sanitize_serial(serial)
    if s:
        per = config_path(s, name)
        if per.exists():
            return per
    return config_path(name)


def signing_write_path(name: str, serial: str | None = None) -> Path:
    """Where to WRITE a per-printer signing file. With a serial, writes into
    ``<config dir>/<serial>/`` (created by the caller) so the install becomes
    multi-printer-safe going forward; without one, the global path (legacy)."""
    s = _sanitize_serial(serial)
    return config_path(s, name) if s else config_path(name)


def credentials_path() -> Path:
    """The printer-credentials INI file read by :class:`beambam.config.Creds`
    and edited by ``beambam config``."""
    raw = os.environ.get("BEAMBAM_CREDS") or os.environ.get("X2D_CREDS")
    return Path(raw).expanduser() if raw else config_path("credentials")


def parse_env_text(text: str) -> dict[str, str]:
    """Parse dotenv-style text into a dict.

    Supports ``KEY=VALUE``, an optional leading ``export``, blank lines,
    ``#`` comments (whole-line, or after an unquoted value preceded by
    whitespace), and single/double-quoted values (double quotes understand
    ``\\n``, ``\\t``, ``\\"`` and ``\\\\``). Malformed lines are skipped.
    """
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = (value.replace("\\\\", "\0").replace("\\n", "\n")
                         .replace("\\t", "\t").replace('\\"', '"')
                         .replace("\0", "\\"))
        else:
            # Inline comment: `VALUE  # note` (the space keeps `a#b` intact).
            hash_at = value.find(" #")
            if hash_at >= 0:
                value = value[:hash_at].rstrip()
        out[key] = value
    return out


def _candidate_env_files() -> list[Path]:
    files: list[Path] = []
    explicit = os.environ.get("BEAMBAM_ENV_FILE")
    if explicit:
        files.append(Path(explicit).expanduser())
    files.append(Path.cwd() / ".env")
    files.append(config_path(".env"))
    return files


def load_env_files(files: list[Path] | None = None) -> list[Path]:
    """Copy beambam's keys from .env files into ``os.environ``.

    Existing environment variables always win; among files the first one
    that defines a key wins. ``BEAMBAM_HOME`` set in ``./.env`` is honoured
    when locating ``<config dir>/.env`` (the dir is re-evaluated per file).
    Returns the files that were actually read. Never raises on a bad or
    unreadable file — configuration problems surface later as the normal
    "credentials missing" error, which names every source.
    """
    read: list[Path] = []
    seen: set[Path] = set()
    pending = list(files) if files is not None else None
    while True:
        if pending is not None:
            if not pending:
                break
            path = pending.pop(0)
        else:
            # Recompute each round so BEAMBAM_HOME from ./.env moves the
            # config-dir .env lookup.
            remaining = [p for p in _candidate_env_files() if p.resolve() not in seen]
            if not remaining:
                break
            path = remaining[0]
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        read.append(path)
        for key, value in parse_env_text(text).items():
            if key.startswith(ENV_PREFIXES) and key not in os.environ:
                os.environ[key] = value
    return read
