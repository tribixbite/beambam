"""Tests for beambam.Printer — high-level facade.

Offline tests focus on construction, lazy MQTT, context-manager
semantics, and method routing. The MQTT/FTPS round-trips themselves
are covered by the live_printer suite (when env is set)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam import Printer
from beambam.config import Creds


FAKE = Creds(ip="192.168.0.999", code="ABCD1234", serial="FAKESERIAL12345")


def test_printer_constructs_with_creds():
    p = Printer(creds=FAKE)
    assert p.creds is FAKE
    assert p._client is None
    assert p._cloud is None


def test_printer_repr_when_disconnected():
    p = Printer(creds=FAKE)
    r = repr(p)
    assert "192.168.0.999" in r
    assert "FAKESERI" in r
    assert "mqtt=closed" in r


def test_printer_is_context_manager():
    """Entering the manager returns self; exiting calls disconnect."""
    p = Printer(creds=FAKE)
    with patch.object(p, "disconnect") as disc:
        with p as ctx:
            assert ctx is p
        assert disc.call_count == 1


def test_disconnect_idempotent_when_never_connected():
    """Calling disconnect on a never-opened Printer is a no-op."""
    p = Printer(creds=FAKE)
    p.disconnect()
    p.disconnect()                                       # again, still no error


# The print-control verbs (pause/resume/stop/gcode/set_temp/home) route
# through beambam.commands.send_command — print.* signed with the recovered
# key — NOT the leaked-cert X2DClient.publish. These tests patch send_command
# and read the payload it was handed (send_command(mqtt, payload, ...)).


def _sent_payloads(send_mock):
    return [c.args[1] for c in send_mock.call_args_list]


def test_pause_resume_stop_call_publish():
    """The three print-control verbs go through send_command in order."""
    p = Printer(creds=FAKE)
    p._client = MagicMock()                              # bypass lazy connect
    with patch("beambam.commands.send_command", return_value=None) as send:
        p.pause();  p.resume();  p.stop()
    assert send.call_count == 3
    payloads = [pl["print"]["command"] for pl in _sent_payloads(send)]
    assert payloads == ["pause", "resume", "stop"]


def test_gcode_appends_newline():
    p = Printer(creds=FAKE)
    p._client = MagicMock()
    with patch("beambam.commands.send_command", return_value=None) as send:
        p.gcode("G28 Z")
        assert _sent_payloads(send)[-1]["print"]["param"] == "G28 Z\n"
        p.gcode("M104 S210\n")                           # already terminated
        assert _sent_payloads(send)[-1]["print"]["param"] == "M104 S210\n"


def test_set_temp_emits_one_gcode_per_axis_set():
    p = Printer(creds=FAKE)
    p._client = MagicMock()
    with patch("beambam.commands.send_command", return_value=None) as send:
        p.set_temp(nozzle=215, bed=60, chamber=45)
    cmds = [pl["print"]["param"].strip() for pl in _sent_payloads(send)]
    assert cmds == ["M104 S215", "M140 S60", "M141 S45"]


def test_set_temp_skips_unset_axes():
    p = Printer(creds=FAKE)
    p._client = MagicMock()
    with patch("beambam.commands.send_command", return_value=None) as send:
        p.set_temp(bed=60)
    cmds = [pl["print"]["param"].strip() for pl in _sent_payloads(send)]
    assert cmds == ["M140 S60"]


def test_chamber_light_on_off():
    p = Printer(creds=FAKE)
    fake_client = MagicMock()
    p._client = fake_client
    p.chamber_light(True)
    assert fake_client.publish.call_args.args[0]["system"]["led_mode"] == "on"
    p.chamber_light(False)
    assert fake_client.publish.call_args.args[0]["system"]["led_mode"] == "off"


def test_home_default_xyz():
    p = Printer(creds=FAKE)
    p._client = MagicMock()
    with patch("beambam.commands.send_command", return_value=None) as send:
        p.home()
        assert _sent_payloads(send)[-1]["print"]["param"].strip() == "G28 X Y Z"
        p.home(axes="Z")
        assert _sent_payloads(send)[-1]["print"]["param"].strip() == "G28 Z"


def test_upload_delegates_to_ftps_helper(tmp_path):
    """Printer.upload calls beambam.ftps.upload_file with our creds + path."""
    fixture = tmp_path / "f.3mf"
    fixture.write_bytes(b"x")
    p = Printer(creds=FAKE)
    with patch("beambam.ftps.upload_file") as upload:
        # cache/ by default: where the X2D LAN print start looks for the file.
        p.upload(fixture)
        upload.assert_called_once_with(FAKE, fixture, remote_name=None, remote_dir="cache")
        upload.reset_mock()
        p.upload(fixture, remote_name="other.3mf", remote_dir=None)
        upload.assert_called_once_with(FAKE, fixture, remote_name="other.3mf", remote_dir=None)


def test_list_files_delegates(tmp_path):
    p = Printer(creds=FAKE)
    with patch("beambam.ftps.list_files", return_value=["a", "b"]) as lf:
        result = p.list_files()
        assert result == ["a", "b"]
        lf.assert_called_once_with(FAKE, "")


# --- live tests (auto-tagged via live_printer fixture) ---------------------


@pytest.mark.live
def test_live_state_request(live_printer):
    """Real-printer round-trip: state() should return a populated dict
    with print.gcode_state set to one of the known values."""
    p = Printer(creds=Creds(ip=live_printer.ip, code=live_printer.code,
                            serial=live_printer.serial))
    with p:
        s = p.state(timeout=10.0)
    assert "print" in s
    gs = s["print"].get("gcode_state")
    assert gs in {"IDLE", "RUNNING", "PAUSE", "FINISH", "FAILED", "PREPARE"}


def test_print_control_goes_through_send_command():
    """pause/stop/gcode use beambam.commands.send_command (print.* signed with
    the recovered key), not the leaked-cert X2DClient.publish."""
    p = Printer(creds=FAKE)
    p._client = MagicMock()
    with patch("beambam.commands.send_command", return_value=None) as send:
        p.pause()
        p.gcode("M400")
    sent = [c.args[1] for c in send.call_args_list]
    assert sent[0]["print"]["command"] == "pause"
    assert sent[1]["print"]["command"] == "gcode_line"
    assert sent[1]["print"]["param"] == "M400\n"
    assert all(c.kwargs["raise_on_refusal"] for c in send.call_args_list)


def test_refused_command_raises():
    import pytest as _pytest
    from beambam.commands import CommandRefused, send_command
    from beambam.state import command_reply

    class _Cli:
        message_seq = 0
        def publish(self, payload): pass
        def wait_for(self, pred, timeout, since=None):
            msg = {"system": {"command": "ledctrl", "result": "failed", "err_code": 7}}
            return msg if pred(msg) else None
    with _pytest.raises(CommandRefused, match="err_code 7"):
        send_command(_Cli(), {"system": {"command": "ledctrl"}}, raise_on_refusal=True)
    assert command_reply({"system": {"command": "ledctrl", "result": "success"}},
                         "system", "ledctrl").ok
