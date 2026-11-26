"""beambam.http_validate — request guards and body validation for the
daemon's HTTP surface (`beambam.serve_http`).

Two jobs:

1. **Cross-site request forgery / DNS rebinding guard.** The daemon's
   default bind is loopback with no token, which used to mean any web
   page the user visited could POST /control/stop: a ``text/plain``
   body was parsed as JSON and nothing looked at ``Origin`` or
   ``Host``. :func:`request_guard_error` now requires

   * a ``Host`` that is an IP literal, ``localhost``, the bind host or a
     configured name — a DNS-rebinding page arrives with its own domain
     in ``Host``;
   * for POST: no cross-origin ``Origin`` / ``Sec-Fetch-Site:
     cross-site``, and a Content-Type a browser cannot send cross-origin
     without a CORS preflight (which the daemon never grants):
     ``application/json`` for JSON routes, a non-"simple" type such as
     ``application/octet-stream`` for raw-body routes.

   Non-browser callers (Home Assistant's publisher, the MCP server,
   curl with ``-H 'Content-Type: application/json'``) send no Origin and
   pass unchanged.

2. **Body validation.** Handlers used to crash (500 / dropped
   connection) on a non-numeric Content-Length, a JSON array body or
   ``{"slot": "x"}``. :class:`BadRequest` plus the typed getters below
   turn every malformed input into one 400 JSON error.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlsplit

__all__ = [
    "BadRequest",
    "HeaderLookup",
    "JSON_MAX_BYTES",
    "RAW_BODY_ROUTES",
    "allowed_host_names",
    "content_length",
    "get_bool",
    "get_float",
    "get_int",
    "get_optional_int",
    "get_str",
    "is_safe_slug",
    "parse_json_object",
    "request_guard_error",
]

# JSON control bodies are tiny; anything bigger is a mistake or abuse.
JSON_MAX_BYTES = 64 * 1024

# POST routes whose body is raw bytes (a model / .gcode.3mf), not JSON.
RAW_BODY_ROUTES: frozenset[str] = frozenset(
    {"/analyze", "/slice-print", "/print/upload", "/slice", "/project/update"})

# Content types a cross-origin page may send WITHOUT a CORS preflight
# (the Fetch standard's "CORS-safelisted" request content types).
_SIMPLE_CONTENT_TYPES = frozenset({
    "text/plain",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
})

# Path components (timelapse printer / job ids) must be plain slugs.
_SLUG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,119}")


class HeaderLookup(Protocol):
    """What the guards read from request headers: satisfied by
    ``http.client.HTTPMessage`` (``handler.headers``) and by a plain
    dict in tests."""

    def get(self, name: str, /) -> str | None: ...


class BadRequest(Exception):
    """A malformed request; the handler answers ``status`` with
    ``{"error": message}``."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --- host / origin guard ------------------------------------------------

def _split_host(host_header: str) -> str:
    """Hostname part of a Host header value (``[::1]:8765`` → ``::1``)."""
    h = host_header.strip()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end > 0 else h
    if h.count(":") == 1:
        return h.rsplit(":", 1)[0]
    return h


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def allowed_host_names(bind_host: str,
                       extra: Iterable[str] | None = None) -> frozenset[str]:
    """Host names (not IP literals, which are always accepted) that the
    daemon answers to: ``localhost``, the machine's own names, the bind
    host when it is a name, ``extra`` and ``$X2D_ALLOWED_HOSTS``
    (comma-separated). A DNS-rebinding attacker's domain is none of
    these."""
    names = {"localhost"}
    try:
        hn = socket.gethostname().strip().lower()
    except OSError:
        hn = ""
    if hn:
        # No socket.getfqdn(): it is a reverse-DNS lookup, which takes ~30 s
        # on macOS without a resolver answer and ran before the daemon bound
        # its port. A fully qualified name goes in --allow-host.
        names.update({hn, f"{hn}.local"})
    b = bind_host.strip().strip("[]").lower()
    if b and not _is_ip_literal(b):
        names.add(b)
    env = os.environ.get("X2D_ALLOWED_HOSTS", "")
    for n in [*env.split(","), *(extra or [])]:
        n = n.strip().lower().rstrip(".")
        if n:
            names.add(n)
    return frozenset(names)


def _host_ok(host_header: str, allowed: frozenset[str]) -> bool:
    if not host_header:
        # HTTP/1.0 without Host: never a browser, nothing to rebind.
        return True
    name = _split_host(host_header).lower().rstrip(".")
    return _is_ip_literal(name) or name in allowed


def _mime(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def request_guard_error(method: str, path: str,
                        headers: HeaderLookup,
                        allowed_hosts: frozenset[str],
                        allowed_origins: frozenset[str] | set[str] | None = None
                        ) -> tuple[int, str] | None:
    """Return ``(status, reason)`` when the request must be refused, else
    None. Applies to every method; the Origin and Content-Type rules
    only to state-changing ones (POST). ``allowed_origins`` (from
    ``--cors-origin``) whitelists browser origins beyond the daemon's own,
    so a listed web app (e.g. https://pokefit.party) may POST."""
    allowed_origins = allowed_origins or frozenset()
    host = headers.get("Host") or ""
    if not _host_ok(host, allowed_hosts):
        return 421, (f"Host {host!r} is not this daemon (DNS rebinding "
                     "guard); add it with --allow-host or $X2D_ALLOWED_HOSTS")
    if method.upper() != "POST":
        return None
    origin = headers.get("Origin")
    origin_allowed = origin is not None and (
        origin.strip().rstrip("/") in allowed_origins or "*" in allowed_origins)
    if (headers.get("Sec-Fetch-Site") or "").lower() == "cross-site" and not origin_allowed:
        return 403, ("cross-site request refused; start the daemon with "
                     "--cors-origin to allow a browser app")
    if origin is not None and not origin_allowed:
        o = origin.strip()
        if o.lower() == "null":
            return 403, "opaque Origin refused"
        netloc = urlsplit(o).netloc.lower()
        if not netloc or netloc != host.strip().lower():
            return 403, (f"origin {o} is not allowed; start the daemon with "
                         f"--cors-origin {o} to allow it")
    mime = _mime(headers.get("Content-Type") or "")
    if path in RAW_BODY_ROUTES:
        if not mime or mime in _SIMPLE_CONTENT_TYPES:
            return 415, ("send the body as application/octet-stream "
                         f"(got {mime or 'no Content-Type'})")
    elif mime != "application/json":
        return 415, ("Content-Type must be application/json "
                     f"(got {mime or 'none'})")
    return None


# --- body parsing ----------------------------------------------------------

def content_length(headers: HeaderLookup, max_bytes: int) -> int:
    """Validated Content-Length (0 when absent). 400 when not a
    non-negative integer, 413 when above `max_bytes`."""
    raw = (headers.get("Content-Length") or "").strip()
    if not raw:
        return 0
    if not raw.isdigit():
        raise BadRequest(f"Content-Length must be an integer, got {raw!r}")
    n = int(raw)
    if n > max_bytes:
        raise BadRequest(f"body too large ({n} B); cap is "
                         f"{_fmt_size(max_bytes)}", status=413)
    return n


def _fmt_size(n: int) -> str:
    """64 MiB / 64 KiB / 123 B — for error messages."""
    if n >= 1 << 20 and n % (1 << 20) == 0:
        return f"{n >> 20} MiB"
    if n >= 1 << 10 and n % (1 << 10) == 0:
        return f"{n >> 10} KiB"
    return f"{n} B"


def parse_json_object(raw: bytes) -> dict[str, Any]:
    """Decode a JSON body that must be an object; empty body → {}."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BadRequest(f"body is not valid JSON: {e}") from None
    if not isinstance(value, dict):
        raise BadRequest("body must be a JSON object, got "
                         f"{type(value).__name__}")
    return value


# --- typed field getters ------------------------------------------------------

_MISSING: Any = object()


def get_int(body: Mapping[str, Any], key: str, default: Any = _MISSING, *,
            lo: int | None = None, hi: int | None = None) -> int:
    """Integer field (an integral float such as 2.0 is accepted; bools
    and strings are not). Missing → `default`, or 400 when required."""
    if key not in body or body[key] is None:
        if default is _MISSING:
            raise BadRequest(f"missing required field {key!r}")
        return int(default)
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadRequest(f"{key!r} must be an integer, got {v!r}")
    if isinstance(v, float):
        if not v.is_integer():
            raise BadRequest(f"{key!r} must be an integer, got {v!r}")
        v = int(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise BadRequest(f"{key!r} must be in {lo}..{hi}, got {v}")
    return v


def get_optional_int(body: Mapping[str, Any], key: str, *,
                     lo: int | None = None,
                     hi: int | None = None) -> int | None:
    """Like :func:`get_int` but None when absent or null."""
    if body.get(key) is None:
        return None
    return get_int(body, key, lo=lo, hi=hi)


def get_float(body: Mapping[str, Any], key: str, default: Any = _MISSING, *,
              lo: float | None = None, hi: float | None = None) -> float:
    """Numeric field (int or float, not bool / str)."""
    if key not in body or body[key] is None:
        if default is _MISSING:
            raise BadRequest(f"missing required field {key!r}")
        return float(default)
    v = body[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise BadRequest(f"{key!r} must be a number, got {v!r}")
    f = float(v)
    if f != f or (lo is not None and f < lo) or (hi is not None and f > hi):
        raise BadRequest(f"{key!r} must be in {lo}..{hi}, got {v}")
    return f


def get_str(body: Mapping[str, Any], key: str, default: Any = _MISSING, *,
            max_len: int = 1024) -> str:
    """String field, at most `max_len` characters."""
    if key not in body or body[key] is None:
        if default is _MISSING:
            raise BadRequest(f"missing required field {key!r}")
        return str(default)
    v = body[key]
    if not isinstance(v, str):
        raise BadRequest(f"{key!r} must be a string, got {type(v).__name__}")
    if len(v) > max_len:
        raise BadRequest(f"{key!r} longer than {max_len} characters")
    return v


def get_bool(body: Mapping[str, Any], key: str, default: bool = False) -> bool:
    """Boolean field (JSON true/false only)."""
    if key not in body or body[key] is None:
        return default
    v = body[key]
    if not isinstance(v, bool):
        raise BadRequest(f"{key!r} must be true or false, got {v!r}")
    return v


def is_safe_slug(value: str) -> bool:
    """True for a plain path component: letters, digits, ``._-``, not
    starting with a dot, at most 120 characters. Rejects ``..``, empty
    strings, separators (including URL-decoded ``%2F``) and drive
    letters."""
    return bool(_SLUG_RE.fullmatch(value))
