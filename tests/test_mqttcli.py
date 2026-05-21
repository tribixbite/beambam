"""Tests for beambam.mqttcli — raw MQTT debug helpers."""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from beambam.mqttcli import (
    add_subparser,
    cmd_mqtt,
    publish_signed,
    subscribe_loop,
)


def _ns(**kw):
    """Build an argparse.Namespace with all the global creds fields plus
    the subcommand-specific ones."""
    defaults = dict(ip=None, code=None, serial=None, printer=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ----- subparser ----------------------------------------------------------


def test_subparser_requires_mqtt_subcommand():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["mqtt"])


def test_subparser_sub_defaults():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["mqtt", "sub"])
    assert args.mqtt_cmd == "sub"
    assert args.topic is None
    assert args.raw is False
    assert args.max_messages is None


def test_subparser_pub_accepts_qos_choice():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    args = p.parse_args(["mqtt", "pub", '{"x":1}', "--qos", "2"])
    assert args.qos == 2
    assert args.payload == '{"x":1}'


def test_subparser_pub_rejects_bad_qos():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    add_subparser(sub)
    with pytest.raises(SystemExit):
        p.parse_args(["mqtt", "pub", "{}", "--qos", "5"])


# ----- cmd_mqtt dispatch -------------------------------------------------


def test_cmd_mqtt_pub_invalid_json(capsys):
    args = _ns(mqtt_cmd="pub", payload="not json", file=None, qos=1)
    with patch("beambam.config.Creds.resolve") as resolve:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 2
    assert "invalid JSON" in capsys.readouterr().err


def test_cmd_mqtt_pub_missing_payload(capsys):
    """Neither positional payload nor --file → error."""
    args = _ns(mqtt_cmd="pub", payload=None, file=None, qos=1)
    with patch("beambam.config.Creds.resolve") as resolve:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 2
    assert "PAYLOAD" in capsys.readouterr().err or "--file" in capsys.readouterr().err


def test_cmd_mqtt_pub_publishes_payload(capsys):
    args = _ns(mqtt_cmd="pub", payload='{"print":{"command":"pause"}}',
               file=None, qos=1)
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.mqttcli.publish_signed") as pub:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 0
    pub.assert_called_once()
    call_kwargs = pub.call_args.kwargs
    assert call_kwargs["payload"] == {"print": {"command": "pause"}}
    assert "published to device/FAKE/request" in capsys.readouterr().out


def test_cmd_mqtt_pub_reads_from_file(tmp_path):
    payload_file = tmp_path / "p.json"
    payload_file.write_text('{"print":{"command":"resume"}}')
    args = _ns(mqtt_cmd="pub", payload=None, file=str(payload_file), qos=1)
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.mqttcli.publish_signed") as pub:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 0
    assert pub.call_args.kwargs["payload"] == {"print": {"command": "resume"}}


def test_cmd_mqtt_pub_reads_from_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"print":{"command":"stop"}}'))
    args = _ns(mqtt_cmd="pub", payload=None, file="-", qos=1)
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.mqttcli.publish_signed") as pub:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 0
    assert pub.call_args.kwargs["payload"] == {"print": {"command": "stop"}}


def test_cmd_mqtt_pub_surfaces_publish_error(capsys):
    args = _ns(mqtt_cmd="pub", payload='{}', file=None, qos=1)
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.mqttcli.publish_signed",
                side_effect=ConnectionError("nope")):
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 1
    assert "publish failed" in capsys.readouterr().err


def test_cmd_mqtt_sub_delegates_to_subscribe_loop():
    args = _ns(mqtt_cmd="sub", topic="device/+/report",
               raw=True, max_messages=3)
    with patch("beambam.config.Creds.resolve") as resolve, \
         patch("beambam.mqttcli.subscribe_loop", return_value=0) as sub:
        from x2d_bridge import Creds as RealCreds
        resolve.return_value = RealCreds(ip="1.2.3.4", code="12345678",
                                          serial="FAKE")
        rc = cmd_mqtt(args)
    assert rc == 0
    sub.assert_called_once()
    kwargs = sub.call_args.kwargs
    assert kwargs["topic"] == "device/+/report"
    assert kwargs["raw"] is True
    assert kwargs["max_messages"] == 3


# ----- live --------------------------------------------------------------


@pytest.mark.live
def test_live_subscribe_receives_one_push(live_printer, tmp_path):
    """Real round-trip: subscribe to /report and receive at least one
    push (triggered by us sending a status request)."""
    from x2d_bridge import Creds
    creds = Creds(ip=live_printer.ip, code=live_printer.code,
                  serial=live_printer.serial)
    out = io.StringIO()
    # The subscribe_loop blocks until max_messages — kick off a status
    # request in parallel to trigger a /report push.
    import threading

    from beambam import Printer

    def _kick():
        import time
        time.sleep(2.0)
        with Printer(creds) as p:
            p.state(timeout=8.0)

    threading.Thread(target=_kick, daemon=True).start()
    rc = subscribe_loop(creds=creds, max_messages=1, raw=True,
                        out_stream=out)
    assert rc == 0
    assert out.getvalue()                                  # got something
