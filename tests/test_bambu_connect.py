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


# --------------------------------------------------------------------------- installing Bambu Connect

import argparse
import os


def _dl_mock(magic=b"MZ"):
    return lambda url, dest, say: (Path(dest).write_bytes(magic), Path(dest))[1]


def _fixed_download(system, machine):
    return lambda **k: bc.Download("2.6.0-beta.0", bc.download_url("2.6.0-beta.0", system, machine))


def test_download_url_and_platform_asset():
    base = "https://public-cdn.bblmw.com/upgrade/bambu-connect/updates/versions"
    assert bc.download_url("2.6.0-beta.0", "Windows", "AMD64") == \
        f"{base}/2.6.0-beta.0/bambu-connect-v2.6.0-beta.0-win32-x64.exe"
    assert bc.download_url("2.6.0-beta.0", "Darwin", "arm64").endswith("darwin-arm64.dmg")
    assert bc.download_url("2.6.0-beta.0", "Darwin", "x86_64").endswith("darwin-x64.dmg")
    assert bc._platform_asset("Linux", "x86_64") is None
    with pytest.raises(bc.ConnectError, match="Windows and macOS only"):
        bc.download_url("2.6.0-beta.0", "Linux", "x86_64")


def test_latest_version_orders_semver_and_prereleases():
    def html(vers):
        return " ".join(f"bambu-connect-v{v}-darwin-arm64.dmg" for v in vers)
    assert bc.latest_version(html(["2.6.0", "2.10.0", "2.6.0"])) == "2.10.0"      # 10 > 6, not string order
    assert bc.latest_version(html(["2.6.0-beta.0", "2.6.0"])) == "2.6.0"          # release > its prerelease
    assert bc.latest_version(html(["2.6.0-beta.0", "2.6.0-rc.0"])) == "2.6.0-rc.0"
    assert bc.latest_version(html(["2.6.0-beta.2", "2.6.0-beta.10"])) == "2.6.0-beta.10"
    assert bc.latest_version(html(["2.99.9", "3.0.0"])) == "3.0.0"
    assert bc.latest_version(html(["2.6", "2.6.1"])) == "2.6.1"                   # more components = newer
    assert bc.latest_version(html(["2.6.0", "2.6.0.1"])) == "2.6.0.1"
    assert bc.latest_version("nothing to see") is None


def test_latest_version_respects_the_platform_token():
    # macOS has a newer build than Windows; a Windows machine must not be offered the macOS-only version.
    html = ("bambu-connect-v2.7.0-darwin-arm64.dmg bambu-connect-v2.6.0-win32-x64.exe "
            "bambu-connect-v2.6.0-darwin-arm64.dmg")
    assert bc.latest_version(html, "win32-x64") == "2.6.0"
    assert bc.latest_version(html, "darwin-arm64") == "2.7.0"
    assert bc.latest_version(html, "darwin-x64") is None


def test_resolve_download_reads_wiki_then_falls_back_to_the_pin():
    page = "bambu-connect-v2.8.1-win32-x64.exe"
    dl = bc.resolve_download(system="Windows", machine="AMD64", fetch=lambda _u: page)
    assert dl.version == "2.8.1" and dl.kind == "exe" and dl.filename.endswith("win32-x64.exe")

    def boom(_u):
        raise RuntimeError("offline")
    dl2 = bc.resolve_download(system="Darwin", machine="arm64", fetch=boom)
    assert dl2.version == bc.PINNED_VERSION and dl2.url.endswith(f"-v{bc.PINNED_VERSION}-darwin-arm64.dmg")

    # A page with only the OTHER platform's newer build falls back to the pin for this one.
    dl3 = bc.resolve_download(system="Windows", machine="AMD64",
                              fetch=lambda _u: "bambu-connect-v9.9.9-darwin-arm64.dmg")
    assert dl3.version == bc.PINNED_VERSION

    with pytest.raises(bc.ConnectError, match="Windows and macOS only"):
        bc.resolve_download(system="Linux", machine="x86_64", fetch=lambda _u: page)


def test_download_refuses_a_foreign_host(tmp_path):
    for bad in ("https://evil.example/x.exe",
                "https://public-cdn.bblmw.com.evil.example/x.exe",      # suffix lookalike
                "http://public-cdn.bblmw.com/x.exe"):                   # not https
        with pytest.raises(bc.ConnectError, match="unexpected host"):
            bc._download(bad, tmp_path / "x", lambda _m: None)
        assert not (tmp_path / "x").exists()


def test_download_rejects_and_cleans_up_a_truncated_file(tmp_path, monkeypatch):
    class Resp:
        headers = {"Content-Length": "100"}
        def geturl(self): return "https://public-cdn.bblmw.com/x.exe"
        def __enter__(self): return self
        def __exit__(self, *a): return False
        _sent = False
        def read(self, _n):
            if self._sent:
                return b""
            self._sent = True
            return b"short"                                            # 5 of 100 bytes, then EOF
    monkeypatch.setattr(bc.urllib.request, "urlopen", lambda *a, **k: Resp())
    dest = tmp_path / "bambu.exe"
    with pytest.raises(bc.ConnectError, match="cut short"):
        bc._download("https://public-cdn.bblmw.com/x.exe", dest, lambda _m: None)
    assert not dest.exists()                                            # partial file removed


def test_download_rejects_a_redirect_off_the_cdn(tmp_path, monkeypatch):
    class Resp:
        headers = {"Content-Length": "2"}
        def geturl(self): return "https://evil.example/x.exe"          # redirected away
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, _n): return b""
    monkeypatch.setattr(bc.urllib.request, "urlopen", lambda *a, **k: Resp())
    with pytest.raises(bc.ConnectError, match="redirected off"):
        bc._download("https://public-cdn.bblmw.com/x.exe", tmp_path / "x", lambda _m: None)


def test_install_on_windows_silently_returns_true_and_cleans_up(monkeypatch, tmp_path):
    monkeypatch.setattr(bc, "_download", _dl_mock())
    monkeypatch.setattr(bc, "resolve_download", _fixed_download("Windows", "AMD64"))
    calls = []

    class R:
        returncode = 0
        stdout = stderr = ""
    completed = bc.install(say=lambda _m: None, system="Windows", machine="AMD64", silent=True,
                           runner=lambda *a, **k: calls.append(a[0]) or R())
    assert completed is True                                           # installed synchronously
    assert calls and calls[0][-1] == "/S"


def test_install_on_windows_interactive_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(bc, "_download", _dl_mock())
    monkeypatch.setattr(bc, "resolve_download", _fixed_download("Windows", "AMD64"))
    started = []
    monkeypatch.setattr(bc.os, "startfile", lambda p: started.append(p), raising=False)
    completed = bc.install(say=lambda _m: None, system="Windows", machine="AMD64", silent=False,
                           dest_dir=tmp_path, runner=lambda *a, **k: None)
    assert completed is False and started                              # wizard launched, not yet installed
    assert list(tmp_path.glob("*.exe"))                                # file kept for the open installer


def test_install_on_windows_raises_when_the_installer_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(bc, "_download", _dl_mock())
    monkeypatch.setattr(bc, "resolve_download", _fixed_download("Windows", "AMD64"))

    class R:
        returncode = 1
        stdout = ""
        stderr = "nope"
    with pytest.raises(bc.ConnectError, match="installer exited with 1"):
        bc.install(say=lambda _m: None, system="Windows", machine="AMD64", silent=True,
                   dest_dir=tmp_path, runner=lambda *a, **k: R())


def test_install_on_macos_mounts_at_our_path_preserves_symlinks_and_detaches(monkeypatch, tmp_path):
    # A fake mounted image holding the app, with an internal symlink like a real .app bundle has.
    mount = tmp_path / "mnt"
    app = mount / "Bambu Connect.app" / "Contents"
    (app / "Frameworks" / "E.framework" / "Versions" / "A").mkdir(parents=True)
    (app / "Info.plist").write_text("<plist/>")
    (app / "Frameworks" / "E.framework" / "Versions" / "Current").symlink_to("A")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(bc.Path, "home", classmethod(lambda cls: home))     # cross-platform (not $HOME)
    monkeypatch.setattr(bc.os, "access", lambda *a, **k: False)             # force the ~/Applications fallback
    monkeypatch.setattr(bc, "_download", _dl_mock(b"dmg"))
    monkeypatch.setattr(bc, "resolve_download", _fixed_download("Darwin", "arm64"))
    seen = []

    class R:
        returncode = 0
        stderr = stdout = ""
    def runner(argv, **k):
        seen.append(argv[:2])
        return R()
    completed = bc.install(say=lambda _m: None, system="Darwin", machine="arm64", dest_dir=tmp_path, runner=runner)
    assert completed is True
    dest = home / "Applications" / "Bambu Connect.app" / "Contents"
    assert (dest / "Info.plist").is_file()
    link = dest / "Frameworks" / "E.framework" / "Versions" / "Current"
    assert link.is_symlink() and os.readlink(link) == "A"                   # seal intact, not dereferenced
    assert ["hdiutil", "attach"] in seen and ["hdiutil", "detach"] in seen


def test_install_on_macos_passes_an_explicit_mountpoint_and_no_quiet(monkeypatch, tmp_path):
    mount = tmp_path / "mnt"
    (mount / "Bambu Connect.app").mkdir(parents=True)
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setattr(bc.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(bc.os, "access", lambda *a, **k: False)
    monkeypatch.setattr(bc, "_download", _dl_mock(b"dmg"))
    monkeypatch.setattr(bc, "resolve_download", _fixed_download("Darwin", "arm64"))
    argvs = []

    class R:
        returncode = 0
        stderr = stdout = ""
    bc.install(say=lambda _m: None, system="Darwin", machine="arm64", dest_dir=tmp_path,
               runner=lambda argv, **k: argvs.append(argv) or R())
    attach = next(a for a in argvs if a[:2] == ["hdiutil", "attach"])
    assert "-mountpoint" in attach and str(mount) in attach and "-quiet" not in attach


def test_cmd_connect_install_only_installs_then_stops(monkeypatch):
    done = []
    monkeypatch.setattr(bc, "installed", lambda: False)
    monkeypatch.setattr(bc, "install", lambda **k: done.append(k.get("silent")) or True)
    args = argparse.Namespace(file=None, name=None, print_url=False, install=True, silent=True)
    assert bc.cmd_connect(args) == 0 and done == [True]


def test_cmd_connect_install_refused_on_linux(monkeypatch, capsys):
    monkeypatch.setattr(bc, "installed", lambda: None)              # no build here
    called = []
    monkeypatch.setattr(bc, "install", lambda **k: called.append(1))
    args = argparse.Namespace(file=Path("a.gcode.3mf"), name=None, print_url=False, install=True, silent=False)
    assert bc.cmd_connect(args) == 1 and called == []              # never tried to install
    assert "Windows and macOS only" in capsys.readouterr().err


def test_cmd_connect_install_with_interactive_windows_does_not_hand_off(monkeypatch, capsys):
    monkeypatch.setattr(bc, "installed", lambda: False)
    monkeypatch.setattr(bc, "install", lambda **k: False)          # wizard launched, not installed
    handed = []
    monkeypatch.setattr(bc, "open_file", lambda *a, **k: handed.append(1))
    args = argparse.Namespace(file=Path("a.gcode.3mf"), name=None, print_url=False, install=True, silent=False)
    assert bc.cmd_connect(args) == 0 and handed == []              # waits for the user to finish
    assert "finish the Bambu Connect installer" in capsys.readouterr().err


def test_cmd_connect_offers_install_when_missing_then_hands_off(monkeypatch):
    states = iter([False, True])                                   # missing, then present after the install
    monkeypatch.setattr(bc, "installed", lambda: next(states, True))
    monkeypatch.setattr(bc, "install", lambda **k: True)           # silent install completes
    handed = []
    monkeypatch.setattr(bc, "open_file", lambda p, n=None: handed.append(p) or "bambu-connect://import-file?x")
    args = argparse.Namespace(file=Path("a.gcode.3mf"), name=None, print_url=False, install=False, silent=True)
    assert bc.cmd_connect(args) == 0 and handed == [Path("a.gcode.3mf")]


def test_cmd_connect_print_url_never_installs(monkeypatch):
    monkeypatch.setattr(bc, "installed", lambda: False)
    called = []
    monkeypatch.setattr(bc, "install", lambda **k: called.append(1) or True)
    monkeypatch.setattr(bc, "open_file", lambda *a, **k: called.append("open"))
    args = argparse.Namespace(file=Path("a.gcode.3mf"), name="n", print_url=True, install=True, silent=False)
    assert bc.cmd_connect(args) == 0 and called == []             # printed only, nothing installed or opened


def test_offer_install_declines_without_a_tty(monkeypatch):
    monkeypatch.setattr(bc.sys.stdin, "isatty", lambda: False)
    called = []
    monkeypatch.setattr(bc, "install", lambda **k: called.append(1))
    assert bc._offer_install(lambda _m: None, silent=False) is bc._DECLINED and called == []


def test_offer_install_handles_eof(monkeypatch):
    monkeypatch.setattr(bc.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: (_ for _ in ()).throw(EOFError()))
    assert bc._offer_install(lambda _m: None, silent=False) is bc._DECLINED


def test_daemon_connect_never_installs(daemon, monkeypatch, tmp_path):
    # POST /connect must hand off only; a web page must never trigger a host install.
    port, _ = daemon
    monkeypatch.setattr(bc, "install", lambda **k: (_ for _ in ()).throw(AssertionError("daemon installed!")))
    monkeypatch.setattr(bc, "problem", lambda: "Bambu Connect is not installed: get it from https://x")
    f = _sliced(tmp_path / "cube.gcode.3mf")
    code, body, _ = _req(port, "/connect", {"file": str(f), "name": "cube"},
                         headers={"Origin": "https://pokefit.party"})
    assert code == 503 and body["ok"] is False and "not installed" in body["error"]


def test_cmd_connect_handles_first_launch_registration_lag(monkeypatch, capsys):
    # Silent install completes, but the URL handler isn't registered until first launch: don't claim failure.
    monkeypatch.setattr(bc, "installed", lambda: False)           # still reports missing after install
    monkeypatch.setattr(bc, "install", lambda **k: True)          # install itself succeeded
    handed = []
    monkeypatch.setattr(bc, "open_file", lambda *a, **k: handed.append(1))
    args = argparse.Namespace(file=Path("a.gcode.3mf"), name=None, print_url=False, install=True, silent=True)
    assert bc.cmd_connect(args) == 0 and handed == []             # no hand-off, no scary error
    assert "open it once and sign in" in capsys.readouterr().err
