"""beambam.bambu_connect: printing with the owner's Bambu login through Bambu Connect, no signing key."""
from __future__ import annotations

import urllib.parse
import zipfile
from pathlib import Path

import pytest

import beambam.bambu_connect as bc
import tests.test_http_print as _http
from tests.test_http_print import _req

daemon = _http.daemon            # the daemon fixture


def _sliced(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/plate_1.gcode", "G28\n")
    return path


def test_the_handoff_url_is_the_documented_one(tmp_path):
    f = _sliced(tmp_path / "my part (v2).gcode.3mf")
    url = bc.handoff_url(f, "Jolteon & co")
    assert url.startswith("bambu-connect://import-file?path=")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert q == {"path": [str(f.resolve())], "name": ["Jolteon & co"], "version": ["1.0.0"]}
    # Every value encoded, the path separators too (/ on POSIX -> %2F, \ on Windows -> %5C).
    assert "%20" in url and ("%2F" in url or "%5C" in url)


def test_only_a_sliced_file_is_handed_over(tmp_path, monkeypatch):
    monkeypatch.setattr(bc, "installed", lambda: True)
    project = tmp_path / "p.3mf"
    with zipfile.ZipFile(project, "w") as z:
        z.writestr("Metadata/project_settings.config", "{}")
    with pytest.raises(bc.ConnectError, match="not sliced"):
        bc.open_file(project)
    with pytest.raises(bc.ConnectError, match="does not exist"):
        bc.open_file(tmp_path / "none.gcode.3mf")
    monkeypatch.setattr(bc, "installed", lambda: None)
    with pytest.raises(bc.ConnectError, match="Windows and macOS only"):
        bc.open_file(_sliced(tmp_path / "a.gcode.3mf"))
    monkeypatch.setattr(bc, "installed", lambda: False)
    assert "not installed" in bc.problem()


def test_post_connect_hands_the_file_over(tmp_path, daemon, monkeypatch):
    port, _ = daemon
    opened: list[tuple[Path, str | None]] = []
    monkeypatch.setattr(bc, "problem", lambda: "")
    monkeypatch.setattr(bc, "open_file", lambda p, n=None: opened.append((p, n)) or bc.handoff_url(p, n))
    f = _sliced(tmp_path / "cube.gcode.3mf")
    code, body, _ = _req(port, "/connect", {"file": str(f), "name": "cube"},
                         headers={"Origin": "https://pokefit.party"})
    assert code == 200 and body["ok"] and body["url"].startswith("bambu-connect://"), body
    assert opened == [(f, "cube")]
    assert _req(port, "/connect", {"name": "x"})[0] == 400
    monkeypatch.setattr(bc, "problem", lambda: "Bambu Connect has no build for this system (Windows and macOS only)")
    code, body, _ = _req(port, "/connect", {"file": str(f)})
    assert code == 503 and "Windows and macOS" in body["error"]
    code, health, _ = _req(port, "/healthz", method="GET")
    assert "bambu-connect" in health["capabilities"] and "Windows" in health["bambu_connect"]
    assert set(health["signing"]) == {"ready", "problem"}
