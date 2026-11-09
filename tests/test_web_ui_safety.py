"""Static + node-run checks on the web UI (web/index.js, web/login.js).

Audit findings covered:

* P1 — stored XSS: renderQueueRow put job.printer (any string accepted
  by /queue/add) into innerHTML unescaped.
* P3 — login.js `?next=//evil.example` was an open redirect.
* P3 — the camera tabs used /cam.jpg, /cam.m3u8 and /cam.webrtc/offer,
  which the HTTP daemon answers 404 (it serves /snapshot.jpg).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
INDEX_JS = (WEB / "index.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")
LOGIN_JS = (WEB / "login.js").read_text(encoding="utf-8")

# Helpers whose output is safe to interpolate into markup.
_SAFE_CALLS = ("escapeText(", "escapeAttr(", "fmtBytes(")


def _innerhtml_templates(src: str) -> list[str]:
    """Every template literal assigned (possibly via `+` chains) to
    .innerHTML."""
    out = []
    for m in re.finditer(r"\.innerHTML\s*=\s*((?:`[^`]*`\s*\+?\s*)+)", src):
        out.extend(re.findall(r"`([^`]*)`", m.group(1)))
    return out


def test_every_innerhtml_interpolation_is_escaped():
    templates = _innerhtml_templates(INDEX_JS)
    assert templates, "found no innerHTML templates — regex out of date?"
    bad = []
    for t in templates:
        for expr in re.findall(r"\$\{([^}]*)\}", t):
            if not expr.strip().startswith(_SAFE_CALLS):
                bad.append(expr)
    assert bad == [], f"unescaped innerHTML interpolations: {bad}"


def test_camera_uses_daemon_routes_only():
    assert '"/snapshot.jpg?t="' in INDEX_JS
    assert '"/cam.jpg?t="' not in INDEX_JS
    for mode in ("hls", "webrtc"):
        tag = re.search(rf'<button[^>]*data-mode="{mode}"[^>]*>', INDEX_HTML)
        assert tag and " hidden" in tag.group(0), tag
    snap = re.search(r'<button[^>]*data-mode="snapshot"[^>]*>', INDEX_HTML)
    assert snap and "hidden" not in snap.group(0)


def test_snapshot_route_exists_in_daemon():
    src = (WEB.parent / "beambam" / "serve_http.py").read_text(encoding="utf-8")
    assert 'path == "/snapshot.jpg"' in src


def _extract_function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_login_next_redirect_stays_on_origin(tmp_path):
    fn = _extract_function(LOGIN_JS, "safeNext")
    cases = ["/index.html?printer=lab#x", "//evil.example/x", "/\\evil.example",
             "https://evil.example/", "javascript:alert(1)", "", None,
             "/%2F%2Fevil.example", "index.html", "/\t/evil.example",
             # dot-segment resolution yields a `//` pathname (bypassed the old fix)
             "/.//evil.example", "/..//evil.example", "/%2e//evil.example",
             "/a/..//evil.example"]
    script = tmp_path / "t.js"
    # Report where the browser would actually go: safeNext's result resolved
    # the way `location.href = result` resolves it.
    script.write_text(
        "const location = { origin: 'http://127.0.0.1:8765' };\n"
        f"{fn}\n"
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map((c) => {\n"
        "  const r = safeNext(c);\n"
        "  return [r, new URL(r, 'http://127.0.0.1:8765/login.html').href];\n"
        "})));\n",
        encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True,
                         text=True, timeout=30, check=True).stdout
    got = dict(zip(map(str, cases), json.loads(out)))
    origin = "http://127.0.0.1:8765/"
    assert got["/index.html?printer=lab#x"][1] == origin + "index.html?printer=lab#x"
    for evil in ("//evil.example/x", "/\evil.example",
                 "https://evil.example/", "javascript:alert(1)", "", "None",
                 "index.html"):
        assert got[evil][0] == "/index.html", (evil, got[evil])
    # Every navigation target stays on our origin, whatever the input.
    for case, (_ret, target) in got.items():
        assert target.startswith(origin), (case, target)
