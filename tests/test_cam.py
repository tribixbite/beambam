"""Tests for beambam.cam — terminal camera viewer."""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.cam import (
    add_subparser,
    cmd_cam,
    detect_backend,
    render,
    render_blocks,
    render_iterm2,
    render_kitty,
    watch_loop,
)


# ----- backend detection ---------------------------------------------------


def test_detect_backend_kitty():
    assert detect_backend({"TERM": "xterm-kitty"}) == "kitty"


def test_detect_backend_ghostty():
    assert detect_backend({"TERM_PROGRAM": "ghostty"}) == "kitty"


def test_detect_backend_iterm2():
    assert detect_backend({"TERM_PROGRAM": "iTerm.app"}) == "iterm2"


def test_detect_backend_lc_terminal():
    assert detect_backend({"LC_TERMINAL": "iTerm2"}) == "iterm2"


def test_detect_backend_fallback_blocks():
    """xterm-256color → blocks (most common case)."""
    assert detect_backend({"TERM": "xterm-256color"}) == "blocks"


def test_detect_backend_empty_env_blocks():
    assert detect_backend({}) == "blocks"


# ----- renderers ----------------------------------------------------------


def _tiny_jpeg() -> bytes:
    """A 2×2 JPEG built via PIL — keep it small to keep tests fast."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("PIL not installed")
    img = Image.new("RGB", (2, 2), (255, 0, 0))
    img.putpixel((1, 0), (0, 255, 0))
    img.putpixel((0, 1), (0, 0, 255))
    img.putpixel((1, 1), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_render_kitty_emits_apc_envelope():
    """Kitty graphics protocol uses ESC_G ... ST."""
    out = render_kitty(b"\xFF\xD8\xFF\xD9")     # minimal valid JPEG bytes
    assert "\033_G" in out
    assert "\033\\" in out
    # Should base64 the jpeg
    import base64
    assert base64.b64encode(b"\xFF\xD8\xFF\xD9").decode("ascii") in out


def test_render_iterm2_emits_osc_1337():
    out = render_iterm2(b"\xFF\xD8\xFF\xD9")
    assert "\033]1337;File=" in out
    assert "\007" in out                                # BEL terminator


def test_render_blocks_uses_ansi_truecolor():
    """Should emit 24-bit color escape sequences + the ▀ half-block."""
    jpeg = _tiny_jpeg()
    out = render_blocks(jpeg, width_chars=8)
    assert "\033[38;2;" in out                          # foreground rgb
    assert "\033[48;2;" in out                          # background rgb
    assert "▀" in out


def test_render_dispatches_by_backend():
    jpeg = _tiny_jpeg()
    assert "\033_G" in render(jpeg, "kitty")
    assert "\033]1337" in render(jpeg, "iterm2")
    assert "▀" in render(jpeg, "blocks", width_chars=8)


def test_render_unknown_backend_raises():
    with pytest.raises(ValueError):
        render(b"\xFF\xD8\xFF\xD9", "sixel")            # type: ignore[arg-type]


# ----- watch_loop --------------------------------------------------------


def test_watch_loop_exits_after_max_frames():
    """Pull 2 frames and stop."""
    jpeg = _tiny_jpeg()
    out = io.StringIO()
    with patch("beambam.cam.fetch_jpeg", return_value=jpeg):
        rc = watch_loop("http://x/", hz=20.0, backend="blocks",
                        width_chars=8, max_frames=2, out_stream=out)
    assert rc == 0
    content = out.getvalue()
    assert "▀" in content                               # at least one rendered frame
    # Cursor hide + show wrappers
    assert "\033[?25l" in content
    assert "\033[?25h" in content


def test_watch_loop_handles_fetch_error_and_continues():
    """A transient HTTP error should print [error] and keep looping."""
    jpeg = _tiny_jpeg()
    out = io.StringIO()
    call_count = [0]

    def flaky(_url, **_kw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("transient")
        return jpeg

    with patch("beambam.cam.fetch_jpeg", side_effect=flaky):
        rc = watch_loop("http://x/", hz=20.0, backend="blocks",
                        width_chars=8, max_frames=2, out_stream=out)
    assert rc == 0
    content = out.getvalue()
    assert "[error] fetch" in content


# ----- cli ----------------------------------------------------------------


def test_subparser_bare_cam_is_snap_default():
    """Bare `beambam cam` now resolves to a snapshot (cam_cmd is None,
    cmd_cam dispatches to _do_snap)."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["cam"])
    assert args.cam_cmd is None       # no sub → snap default
    # parent parser supplies --url default
    assert args.url.startswith("http")


def test_subparser_watch_defaults():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["cam", "watch"])
    assert args.cam_cmd == "watch"
    assert args.hz == 2.0
    assert args.width == 80
    assert args.backend == "auto"
    assert args.max_frames is None


def test_subparser_snap_optional_out():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["cam", "snap"])
    assert args.cam_cmd == "snap"
    assert args.out is None
    args2 = p.parse_args(["cam", "snap", "shot.jpg"])
    assert args2.out == "shot.jpg"


def test_cmd_cam_snap_writes_file(tmp_path, capsys):
    out = tmp_path / "x.jpg"
    args = argparse.Namespace(cam_cmd="snap",
                               url="http://x/",
                               out=str(out))
    with patch("beambam.cam.fetch_jpeg", return_value=b"\xFF\xD8\xFF\xD9"):
        rc = cmd_cam(args)
    assert rc == 0
    assert out.read_bytes() == b"\xFF\xD8\xFF\xD9"
    assert "saved" in capsys.readouterr().out


def test_cmd_cam_snap_default_filename(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(cam_cmd="snap", url="http://x/", out=None)
    with patch("beambam.cam.fetch_jpeg", return_value=b"\xFF\xD8\xFF\xD9"):
        rc = cmd_cam(args)
    assert rc == 0
    assert (tmp_path / "cam.jpg").exists()


def test_cmd_cam_snap_handles_fetch_error(capsys):
    args = argparse.Namespace(cam_cmd="snap", url="http://x/", out="/tmp/x.jpg")
    with patch("beambam.cam.fetch_jpeg", side_effect=OSError("connection refused")):
        rc = cmd_cam(args)
    assert rc == 1
    assert "snapshot failed" in capsys.readouterr().err


# ----- cam start / stop (background proxy) -------------------------------


def test_subparser_cam_start_forwards_flags():
    """`cam start --bind X --port Y --proto Z` parses + populates argv."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["cam", "start",
                          "--bind", "0.0.0.0:8888",
                          "--port", "1234",
                          "--proto", "local",
                          "--idle-timeout", "60",
                          "--skip-check"])
    assert args.cam_cmd == "start"
    assert args.bind == "0.0.0.0:8888"
    assert args.port == 1234
    assert args.proto == "local"
    assert args.idle_timeout == 60.0
    assert args.skip_check is True
    assert args.foreground is False


def test_subparser_cam_stop_default_signal():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["cam", "stop"])
    assert args.cam_cmd == "stop"
    assert args.signal == "TERM"


def test_cam_stop_no_pidfile_returns_1(tmp_path, capsys, monkeypatch):
    """No PID file → clean error, exit 1."""
    monkeypatch.setattr("beambam.cam._CAM_PID_FILE", tmp_path / "missing.pid")
    args = argparse.Namespace(cam_cmd="stop", signal="TERM")
    rc = cmd_cam(args)
    assert rc == 1
    assert "no PID file" in capsys.readouterr().err


def test_cam_stop_sends_signal_and_removes_pidfile(tmp_path, capsys,
                                                     monkeypatch):
    """Happy path: PID file exists, os.kill called, file removed."""
    pid_file = tmp_path / "cam.pid"
    pid_file.write_text("4242\n")
    monkeypatch.setattr("beambam.cam._CAM_PID_FILE", pid_file)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr("os.kill", lambda pid, sig: sent.append((pid, sig)))
    rc = cmd_cam(argparse.Namespace(cam_cmd="stop", signal="TERM"))
    assert rc == 0
    assert len(sent) == 1 and sent[0][0] == 4242
    assert not pid_file.exists()
    assert "sent SIGTERM" in capsys.readouterr().out


def test_cam_stop_stale_pidfile_still_cleans_up(tmp_path, capsys,
                                                  monkeypatch):
    """If the PID isn't a live process, the route still removes the
    stale file rather than leaving it to confuse the next `cam start`."""
    pid_file = tmp_path / "cam.pid"
    pid_file.write_text("9999999\n")
    monkeypatch.setattr("beambam.cam._CAM_PID_FILE", pid_file)
    def _raise(*a, **kw): raise ProcessLookupError(3)
    monkeypatch.setattr("os.kill", _raise)
    rc = cmd_cam(argparse.Namespace(cam_cmd="stop", signal="TERM"))
    assert rc == 0
    assert not pid_file.exists()
    assert "stale PID file" in capsys.readouterr().err


def test_cam_stop_unknown_signal_returns_2(tmp_path, monkeypatch, capsys):
    """`cam stop --signal NONSENSE` should refuse rather than silent-fail."""
    pid_file = tmp_path / "cam.pid"
    pid_file.write_text("4242\n")
    monkeypatch.setattr("beambam.cam._CAM_PID_FILE", pid_file)
    rc = cmd_cam(argparse.Namespace(cam_cmd="stop", signal="NONSENSE"))
    assert rc == 2
    assert "unknown signal" in capsys.readouterr().err


def test_cam_start_blocks_double_start(tmp_path, monkeypatch, capsys):
    """If a live PID exists in the file, `cam start` refuses."""
    pid_file = tmp_path / "cam.pid"
    pid_file.write_text("1111\n")
    monkeypatch.setattr("beambam.cam._CAM_PID_FILE", pid_file)
    # Make os.kill(pid, 0) treat the PID as alive (no exception).
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    args = argparse.Namespace(
        cam_cmd="start", bind="127.0.0.1:8766", port=322, proto="rtsp",
        idle_timeout=30.0, skip_check=False, auth_token="",
        foreground=False)
    rc = cmd_cam(args)
    assert rc == 1
    assert "already running" in capsys.readouterr().err
