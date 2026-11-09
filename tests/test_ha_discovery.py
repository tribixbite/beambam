"""Home Assistant discovery payloads and entity templates (runtime/ha/publisher.py).

Three layers:

* payload structure — every discovery config is valid for HA >= 2025.10
  (``default_entity_id``, no ``object_id``, origin, availability, enum
  options, light state handling);
* template rendering — each ``value_template`` is rendered with Jinja2 (the
  engine HA uses) against a real X2D state captured 2026-11-09
  (``tests/fixtures/x2d_state_ha.json``). Skipped when jinja2 is missing
  (it is in the ``dev`` extra);
* an end-to-end run against an in-process amqtt broker and a real daemon
  HTTP server: merged state on the state topic, birth-message republish,
  printer availability from /healthz and legacy-config cleanup.
"""
from __future__ import annotations

import asyncio
import copy
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from runtime.ha import publisher as ha
from runtime.ha.publisher import Entity, HAPublisher

FIXTURE = Path(__file__).parent / "fixtures" / "x2d_state_ha.json"
SERIAL = "00M00A000000000"
FIXED_NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

# Components whose discovery config carries the shared state topic.
STATEFUL = {"sensor", "binary_sensor", "switch", "number", "light", "update"}


def _x2d_state() -> dict[str, Any]:
    doc = json.loads(FIXTURE.read_text(encoding="utf-8"))
    doc.pop("_comment", None)
    return doc


def _publisher(model: str = "X2D", **kw: Any) -> HAPublisher:
    """A publisher that never connects; publishes are recorded instead."""
    pub = HAPublisher(broker_host="127.0.0.1", broker_port=1, printer_name="studio",
                      device_serial=SERIAL, device_model=model, **kw)
    pub.sent = []  # type: ignore[attr-defined]

    def _record(topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> None:
        pub.sent.append((topic, payload, retain))  # type: ignore[attr-defined]
    pub._client.publish = _record  # type: ignore[method-assign]
    return pub


def _all_entities(model: str = "X2D") -> list[Entity]:
    return ha.build_entities(model, "x2d/" + SERIAL, ams_units=(0, 1, 2, 3))


def _template_of(payload: dict[str, Any]) -> str | None:
    return payload.get("value_template") or payload.get("state_value_template")


# ---------------------------------------------------------------------------
# Payload structure
# ---------------------------------------------------------------------------

class TestDiscoveryPayloads:
    def setup_method(self) -> None:
        self.pub = _publisher()
        self.pub._ams_units = (0, 1, 2, 3)
        self.payloads = {(e.component, e.key): self.pub._discovery_payload(e)
                         for e in self.pub.entities}

    def test_default_entity_id_replaces_object_id(self) -> None:
        for (component, key), p in self.payloads.items():
            assert "object_id" not in p, key
            # Same entity_id the old object_id produced: <component>.x2d_<serial>_<key>
            assert p["default_entity_id"] == f"{component}.x2d_{SERIAL}_{key}"
            assert p["unique_id"] == f"x2d_{SERIAL}_{key}"

    def test_unique_ids_and_names_are_unique(self) -> None:
        uids = [p["unique_id"] for p in self.payloads.values()]
        assert len(uids) == len(set(uids))
        names = [p["name"] for p in self.payloads.values()]
        assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)

    def test_origin_and_device_block(self) -> None:
        p = self.payloads[("sensor", "nozzle_temp")]
        assert p["origin"]["name"] == "beambam"
        assert p["origin"]["support_url"].startswith("https://")
        dev = p["device"]
        assert dev["identifiers"] == [f"x2d_{SERIAL}"]
        assert dev["serial_number"] == SERIAL
        assert dev["manufacturer"] == "Bambu Lab" and dev["model"] == "X2D"
        # No placeholder firmware: sw_version only once the printer reports it.
        assert "sw_version" not in dev
        # Loopback daemon URL is useless as a link from HA's browser.
        assert "configuration_url" not in dev

    def test_configuration_url_for_reachable_daemon(self) -> None:
        pub = _publisher(daemon_url="http://192.168.1.20:8765")
        assert pub._device_block()["configuration_url"] == "http://192.168.1.20:8765"

    def test_firmware_becomes_sw_version(self) -> None:
        self.pub.ingest_state({"info": {"command": "get_version",
                                        "module": [{"name": "ota", "sw_ver": "01.02.00.83"}]}})
        assert self.pub._device_block()["sw_version"] == "01.02.00.83"

    def test_availability(self) -> None:
        base = f"x2d/{SERIAL}"
        for (component, key), p in self.payloads.items():
            if key == "online":
                # Shows the printer link itself; only the bridge can make it unavailable.
                assert p["availability_topic"] == f"{base}/availability"
                assert p["state_topic"] == f"{base}/printer_availability"
                assert (p["payload_on"], p["payload_off"]) == ("online", "offline")
                continue
            assert p["availability_mode"] == "all", key
            assert [a["topic"] for a in p["availability"]] == [
                f"{base}/availability", f"{base}/printer_availability"], key

    def test_camera_and_image_have_availability(self) -> None:
        assert "availability" in self.payloads[("image", "snapshot")]
        assert "availability" in self.payloads[("camera", "chamber_camera")]

    def test_light_has_state(self) -> None:
        p = self.payloads[("light", "chamber_light")]
        assert p["state_topic"] == f"x2d/{SERIAL}/state"
        assert "state_value_template" in p and "value_template" not in p
        assert (p["payload_on"], p["payload_off"]) == ("ON", "OFF")
        assert p["command_topic"] == f"x2d/{SERIAL}/light/set"

    def test_stateful_components_have_state_topic(self) -> None:
        for (component, key), p in self.payloads.items():
            if component in STATEFUL and key != "online":
                assert p["state_topic"] == f"x2d/{SERIAL}/state", key
                assert _template_of(p), key

    def test_enum_sensors(self) -> None:
        enums = {k: p for (c, k), p in self.payloads.items() if p.get("device_class") == "enum"}
        assert {"stage", "active_nozzle", "speed_profile"} <= set(enums)
        for key, p in enums.items():
            assert p["options"], key
            assert "state_class" not in p and "unit_of_measurement" not in p, key

    def test_diagnostic_category(self) -> None:
        for key in ("wifi", "ip_address", "firmware_version", "online", "firmware"):
            comp = {"online": "binary_sensor", "firmware": "update"}.get(key, "sensor")
            assert self.payloads[(comp, key)]["entity_category"] == "diagnostic"

    def test_update_entity_is_read_only(self) -> None:
        p = self.payloads[("update", "firmware")]
        assert "command_topic" not in p and "payload_install" not in p
        assert p["device_class"] == "firmware"

    def test_ams_coverage(self) -> None:
        keys = {k for _, k in self.payloads}
        for slot in range(1, 17):
            assert {f"ams_slot{slot}_color", f"ams_slot{slot}_material",
                    f"ams_slot{slot}_load"} <= keys
        for unit in range(4):
            assert {f"ams_unit{unit}_humidity", f"ams_unit{unit}_humidity_level",
                    f"ams_unit{unit}_temp", f"ams_unit{unit}_humidity_warn"} <= keys
        assert {"ext_spool_color", "ext_spool_255_color"} <= keys
        load = self.payloads[("button", "ams_slot16_load")]
        assert load["command_topic"] == f"x2d/{SERIAL}/ams/16/load"
        assert self.payloads[("sensor", "ams_unit0_humidity")]["unit_of_measurement"] == "%"

    def test_no_ams_entities_before_state(self) -> None:
        keys = {e.key for e in _publisher().entities}
        assert not any(k.startswith(("ams_slot", "ams_unit")) for k in keys)

    def test_model_gating(self) -> None:
        x2d = {e.key for e in _all_entities("X2D")}
        p1s = {e.key for e in _all_entities("P1S")}
        assert {"left_nozzle_temp", "right_nozzle_target", "active_nozzle",
                "ext_spool_255_color"} <= x2d
        assert not ({"left_nozzle_temp", "active_nozzle", "ext_spool_255_color"} & p1s)
        x1c_door = next(e for e in _all_entities("X1C") if e.key == "door_open")
        assert "home_flag" in x1c_door.value_template
        x2d_door = next(e for e in _all_entities("X2D") if e.key == "door_open")
        assert ".stat" in x2d_door.value_template and "hw_switch_state" not in x2d_door.value_template

    def test_retired_entities_cleared_with_empty_retained(self) -> None:
        self.pub._clear_retired()
        cleared = {t for t, payload, retain in self.pub.sent if payload == "" and retain}
        assert f"homeassistant/sensor/x2d_{SERIAL}/queue_pending/config" in cleared
        live = {(e.component, e.key) for e in self.pub.entities}
        assert not (set(ha.RETIRED_ENTITIES) & live)


# ---------------------------------------------------------------------------
# Merged state
# ---------------------------------------------------------------------------

def _state_messages(pub: HAPublisher) -> list[dict[str, Any]]:
    return [json.loads(p) for t, p, _ in pub.sent  # type: ignore[attr-defined]
            if t == pub._state_topic()]


class TestMergedState:
    def test_delta_does_not_wipe_state(self) -> None:
        pub = _publisher()
        pub.ingest_state(_x2d_state())
        pub.ingest_state({"print": {"command": "push_status", "mc_percent": 57,
                                    "nozzle_temper": 219.0}})
        last = _state_messages(pub)[-1]["print"]
        assert last["mc_percent"] == 57 and last["nozzle_temper"] == 219.0
        # Fields absent from the delta survive.
        assert last["gcode_state"] == "FINISH"
        assert len(last["ams"]["ams"]) == 4
        assert last["upgrade_state"]["sn"] == SERIAL

    def test_nothing_published_without_print(self) -> None:
        pub = _publisher()
        pub.ingest_state({"info": {"command": "get_version", "module": []}})
        assert _state_messages(pub) == []

    def test_new_ams_unit_republishes_discovery(self) -> None:
        pub = _publisher()
        pub._connected.set()
        pub.ingest_state(_x2d_state())
        topics = [t for t, _, _ in pub.sent]  # type: ignore[attr-defined]
        assert f"homeassistant/sensor/x2d_{SERIAL}/ams_slot16_color/config" in topics
        before = len(pub.sent)  # type: ignore[attr-defined]
        pub.ingest_state({"print": {"mc_percent": 1}})          # nothing new
        assert all(not t.startswith("homeassistant/")
                   for t, _, _ in pub.sent[before:])  # type: ignore[attr-defined]

    def test_pure_helpers(self) -> None:
        state = _x2d_state()
        assert ha.ams_unit_ids(state) == (0, 1, 2, 3)
        assert ha.serial_from_state(state) == SERIAL
        assert ha.firmware_version(state) is None
        state["info"] = {"module": [{"name": "ota", "sw_ver": "01.02.00.83"}]}
        assert ha.firmware_version(state) == "01.02.00.83"
        assert ha.model_key("H2D Pro") == "H2DPRO"


# ---------------------------------------------------------------------------
# Template rendering (Jinja2, like Home Assistant)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def render() -> Callable[[str, dict[str, Any]], str]:
    """Render an HA template. Stand-ins for HA's own globals/filters:
    to_json, as_datetime, now, timedelta. The default `Undefined` raises on
    attribute access, so a template that would error in HA errors here."""
    jinja2 = pytest.importorskip("jinja2")
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    env = ImmutableSandboxedEnvironment(undefined=jinja2.Undefined)
    env.filters["to_json"] = lambda v: json.dumps(v)
    env.globals.update(
        as_datetime=lambda t: datetime.fromtimestamp(int(t), tz=timezone.utc),
        now=lambda: FIXED_NOW, timedelta=timedelta)

    def _render(template: str, value_json: dict[str, Any]) -> str:
        return env.from_string(template).render(value_json=value_json).strip()
    return _render


def _by_key(model: str = "X2D") -> dict[str, Entity]:
    return {e.key: e for e in _all_entities(model)}


def _attrs_template(entity: Entity) -> str | None:
    return entity.extra.get("json_attributes_template")


X2D_EXPECTED = {
    "nozzle_temp": "28.0",
    "nozzle_target": "0.0",
    "left_nozzle_temp": "28",           # extruder id 1
    "right_nozzle_temp": "27",          # extruder id 0
    "left_nozzle_target": "0",
    "right_nozzle_target": "0",
    "active_nozzle": "right",           # extruder.state = 2 -> index 0
    "bed_temp": "25",                   # device.bed.info.temp
    "bed_target": "0",
    "chamber_temp": "25",               # device.ctc.info.temp (chamber_temper is null)
    "progress": "100",
    "current_layer": "121",
    "total_layers": "121",
    "remaining": "0",
    "wifi": "-60",
    "stage": "finish",
    "speed_profile": "standard",
    "hms_count": "3",
    "ip_address": "192.168.1.50",
    "firmware_version": "None",         # no get_version reply in the capture
    "start_time": "None",               # gcode_start_time is null on X2D
    "end_time": "None",                 # job finished
    "nozzle_diameter": "0.4",
    "nozzle_type": "HS01",
    "door_open": "OFF",                 # stat 258000: bit 23 clear
    "hms_problem": "ON",
    "print_error": "OFF",
    "timelapse_running": "OFF",
    "chamber_light": "ON",
    "light": "on",
    "ams_slot1_color": "#F72323",
    "ams_slot1_material": "PETG",
    "ams_slot16_color": "#FF0000",
    "ams_slot16_material": "PLA",
    "ams_unit0_humidity": "40",
    "ams_unit1_humidity": "39",
    "ams_unit0_humidity_level": "4",    # raw 2 -> level D
    "ams_unit0_temp": "24.1",
    "ams_unit0_humidity_warn": "ON",
    "ext_spool_color": "#898989",
    "ext_spool_material": "PLA",
    "ext_spool_255_material": "PLA",
    "active_tray": "0",
    "firmware": "",                     # update: '' is ignored by HA
    "bed_set": "0",
    "chamber_set": "0",
}


class TestTemplates:
    def test_x2d_capture(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        state = _x2d_state()
        ents = _by_key()
        got = {k: render(ents[k].value_template, state) for k in X2D_EXPECTED}
        assert got == X2D_EXPECTED

    @pytest.mark.parametrize("model", ["X2D", "P1S", "X1C"])
    @pytest.mark.parametrize("which", ["capture", "empty_print", "single_nozzle"])
    def test_every_template_renders(self, render: Callable[[str, dict[str, Any]], str],
                                    model: str, which: str) -> None:
        state = {"capture": _x2d_state(), "empty_print": {"print": {}},
                 "single_nozzle": {"print": {"nozzle_temper": 210.4, "bed_temper": 60.2,
                                             "bed_target_temper": 60, "chamber_temper": 31,
                                             "home_flag": -1058546177,
                                             "gcode_state": "RUNNING",
                                             "vt_tray": {"id": "254", "tray_type": "PETG",
                                                         "tray_color": "112233FF"}}}}[which]
        for e in _all_entities(model):
            for tpl in filter(None, (e.value_template, _attrs_template(e))):
                out = render(tpl, state)
                if tpl is _attrs_template(e):
                    assert isinstance(json.loads(out), dict), e.key
                    continue
                if e.component == "sensor" and (e.unit or e.state_class or e.device_class):
                    # '' is ignored by HA and leaves a stale value: must be a value or None.
                    assert out != "", e.key
                if e.device_class == "enum":
                    assert out == "None" or out in e.extra["options"], (e.key, out)
                if e.device_class == "timestamp" and out != "None":
                    datetime.fromisoformat(out)
                if e.component == "binary_sensor":
                    assert out in ("ON", "OFF", "None"), (e.key, out)

    def test_single_nozzle_fallbacks(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        state = {"print": {"bed_temper": 60.24, "bed_target_temper": 60, "chamber_temper": 31,
                           "home_flag": -1058546177,   # X1C, door open (ha-bambulab sample)
                           "vt_tray": {"id": "254", "tray_type": "PETG", "tray_color": "112233FF"}}}
        x1c = _by_key("X1C")
        assert render(x1c["bed_temp"].value_template, state) == "60.2"
        assert render(x1c["bed_target"].value_template, state) == "60"
        assert render(x1c["chamber_temp"].value_template, state) == "31"
        assert render(x1c["door_open"].value_template, state) == "ON"
        state["print"]["home_flag"] = -1066934785                  # closed
        assert render(x1c["door_open"].value_template, state) == "OFF"
        assert render(x1c["ext_spool_color"].value_template, state) == "#112233"

    def test_door_open_on_stat_bit(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        tpl = _by_key()["door_open"].value_template
        assert render(tpl, {"print": {"stat": "A58000"}}) == "ON"
        assert render(tpl, {"print": {"stat": "46A58008"}}) == "ON"
        assert render(tpl, {"print": {"stat": "46258008"}}) == "OFF"
        # hw_switch_state is the filament sensor, not the door.
        assert render(tpl, {"print": {"stat": "258000", "hw_switch_state": 1}}) == "OFF"

    def test_running_job(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        state = _x2d_state()
        state["print"].update(gcode_state="RUNNING", mc_remaining_time=30,
                              gcode_start_time="1762689600")
        state["print"]["device"]["extruder"]["state"] = 0x12   # 2 nozzles, left active
        state["print"]["device"]["extruder"]["info"][1]["temp"] = (220 << 16) | 218
        ents = _by_key()
        assert render(ents["end_time"].value_template, state) == \
            (FIXED_NOW + timedelta(minutes=30)).isoformat()
        assert render(ents["start_time"].value_template, state) == \
            datetime.fromtimestamp(1762689600, tz=timezone.utc).isoformat()
        assert render(ents["active_nozzle"].value_template, state) == "left"
        assert render(ents["left_nozzle_temp"].value_template, state) == "218"
        assert render(ents["left_nozzle_target"].value_template, state) == "220"
        assert render(ents["stage"].value_template, state) == "running"

    def test_firmware_update_payload(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        state = _x2d_state()
        state["info"] = {"command": "get_version",
                         "module": [{"name": "ota", "sw_ver": "01.02.00.70"}]}
        ents = _by_key()
        out = json.loads(render(ents["firmware"].value_template, state))
        # upgrade_state.new_version_state == 1 offers ota_new_version_number.
        assert out["installed_version"] == "01.02.00.70"
        assert out["latest_version"] == "01.02.00.83"
        assert out["release_url"].endswith("/firmware-download/x2")
        assert render(ents["firmware_version"].value_template, state) == "01.02.00.70"
        state["print"]["upgrade_state"]["new_version_state"] = 2
        out = json.loads(render(ents["firmware"].value_template, state))
        assert out["latest_version"] == "01.02.00.70"

    def test_attribute_templates(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        state = _x2d_state()
        ents = _by_key()
        hms = json.loads(render(_attrs_template(ents["hms_problem"]) or "", state))
        assert hms["count"] == 3
        # attr 83952640 = 0x05010400, code 196610 = 0x00030002
        assert hms["codes"][0] == "0501_0400_0003_0002"
        tray = json.loads(render(_attrs_template(ents["ams_slot8_material"]) or "", state))
        assert tray["remain"] == 71 and tray["sub_brand"] == "PLA Pure"
        assert tray["filament_id"] == "GFA19"

    def test_light_flashing_and_missing(self, render: Callable[[str, dict[str, Any]], str]) -> None:
        tpl = _by_key()["chamber_light"].value_template
        flashing = {"print": {"lights_report": [{"node": "chamber_light", "mode": "flashing"}]}}
        assert render(tpl, flashing) == "ON"
        off = {"print": {"lights_report": [{"node": "work_light", "mode": "on"},
                                           {"node": "chamber_light", "mode": "off"}]}}
        assert render(tpl, off) == "OFF"
        assert render(tpl, {"print": {}}) == "None"


# ---------------------------------------------------------------------------
# `beambam ha`: the identity is always the serial
# ---------------------------------------------------------------------------

def _ha_args(**over: Any) -> Any:
    import argparse
    base = dict(printer="", device_serial="", broker="127.0.0.1:1883",
                broker_username="", broker_password="", daemon_url="http://127.0.0.1:1",
                daemon_token="", discovery_prefix="homeassistant", device_model="X2D",
                ha_status_topic="homeassistant/status", configuration_url="")
    base.update(over)
    return argparse.Namespace(**base)


class _CapturePublisher:
    """Records constructor kwargs; start() fails so cmd_ha_publish returns."""
    seen: list[dict[str, Any]] = []

    def __init__(self, **kw: Any) -> None:
        _CapturePublisher.seen.append(kw)

    def start(self) -> None:
        raise RuntimeError("not starting in tests")


@pytest.mark.parametrize("printer", ["", "studio"])
def test_ha_command_uses_serial_not_name(monkeypatch: pytest.MonkeyPatch, printer: str) -> None:
    import argparse
    from beambam.cli.daemon import cmd_ha_publish
    from beambam.config import Creds

    _CapturePublisher.seen = []
    monkeypatch.setattr(ha, "HAPublisher", _CapturePublisher)
    monkeypatch.setattr(Creds, "list_names", staticmethod(lambda *a: ["studio"]))
    monkeypatch.setattr(Creds, "resolve", classmethod(
        lambda cls, ns: argparse.Namespace(serial=SERIAL)))
    assert cmd_ha_publish(_ha_args(printer=printer)) == 2      # start() refused
    (kw,) = _CapturePublisher.seen
    assert kw["device_serial"] == SERIAL
    assert kw["printer_name"] == "studio"
    assert kw["legacy_device_ids"] == ("x2d_studio",)


def test_ha_command_falls_back_to_daemon_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    from beambam.cli.daemon import cmd_ha_publish
    from beambam.config import Creds

    def _no_creds(cls: Any, ns: Any) -> Any:
        raise SystemExit(2)
    _CapturePublisher.seen = []
    monkeypatch.setattr(ha, "HAPublisher", _CapturePublisher)
    monkeypatch.setattr(Creds, "resolve", classmethod(_no_creds))
    monkeypatch.setattr(ha, "fetch_serial_from_daemon", lambda *a, **k: "FROMDAEMON1")
    assert cmd_ha_publish(_ha_args(printer="lab")) == 2
    assert _CapturePublisher.seen[0]["device_serial"] == "FROMDAEMON1"
    # Neither source knows it: refuse instead of inventing an id from the name.
    _CapturePublisher.seen = []
    monkeypatch.setattr(ha, "fetch_serial_from_daemon", lambda *a, **k: "")
    assert cmd_ha_publish(_ha_args(printer="lab")) == 2
    assert _CapturePublisher.seen == []


# ---------------------------------------------------------------------------
# End to end: amqtt broker + daemon HTTP server + publisher
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Broker:
    """amqtt broker on its own event loop thread."""

    def __init__(self, port: int) -> None:
        from amqtt.broker import Broker
        self.port = port
        self._cls = Broker
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        broker = self._cls({"listeners": {"default": {"type": "tcp",
                                                      "bind": f"127.0.0.1:{self.port}"}},
                            "auth": {"allow-anonymous": True},
                            "topic-check": {"enabled": False}}, loop=self._loop)
        self._loop.run_until_complete(broker.start())
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(broker.shutdown())

    def __enter__(self) -> "_Broker":
        self._thread.start()
        assert self._ready.wait(10), "amqtt broker did not start"
        return self

    def __exit__(self, *_: object) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)


class _Sniffer:
    def __init__(self, port: int) -> None:
        import paho.mqtt.client as mqtt
        self.retained: dict[str, bytes] = {}
        self.history: list[tuple[str, bytes]] = []
        # Re-entrant: wait() holds it while a predicate may call count().
        self._lock = threading.RLock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sniff-{port}")
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port, keepalive=15)
        self.client.subscribe([("homeassistant/#", 1), ("x2d/#", 1)])
        self.client.loop_start()

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        with self._lock:
            self.retained[msg.topic] = msg.payload
            self.history.append((msg.topic, msg.payload))

    def count(self, topic: str) -> int:
        with self._lock:
            return sum(1 for t, _ in self.history if t == topic)

    def wait(self, pred: Callable[[], bool], timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if pred():
                    return True
            time.sleep(0.05)
        return False

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.fixture
def e2e() -> Iterator[dict[str, Any]]:
    pytest.importorskip("amqtt")
    import paho.mqtt.client as mqtt
    from beambam import _WEB_DIR_DEFAULT
    from beambam.serve_http import _serve_http
    from beambam.state_hub import StateHub

    broker_port, daemon_port = _free_port(), _free_port()
    hub = StateHub(maxqueue=16)
    last_ts = {"value": time.time()}
    threading.Thread(target=_serve_http, daemon=True, kwargs={
        "bind": f"127.0.0.1:{daemon_port}", "get_state": lambda _p: hub.last_state,
        "get_last_ts": lambda _p: last_ts["value"], "max_staleness": 30.0,
        "auth_token": None, "printer_names": ["studio"], "clients": {},
        "web_dir": _WEB_DIR_DEFAULT, "get_hub": lambda _p: hub}).start()
    with _Broker(broker_port):
        # A retained config from the pre-serial device id (started by name).
        seed = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="seed")
        seed.connect("127.0.0.1", broker_port)
        seed.loop_start()
        seed.publish("homeassistant/sensor/x2d_studio/nozzle_temp/config",
                     json.dumps({"name": "old"}), qos=1, retain=True).wait_for_publish(5)
        sniffer = _Sniffer(broker_port)
        pub = HAPublisher(broker_host="127.0.0.1", broker_port=broker_port,
                          daemon_url=f"http://127.0.0.1:{daemon_port}",
                          printer_name="studio", device_serial=SERIAL,
                          legacy_device_ids=("x2d_studio",))
        pub.snapshot_period = 3600
        pub.health_period = 0.2
        pub.birth_delay_max = 0.0
        pub.start()
        try:
            yield {"pub": pub, "hub": hub, "sniffer": sniffer, "seed": seed,
                   "last_ts": last_ts}
        finally:
            pub.stop()
            sniffer.close()
            seed.loop_stop()
            seed.disconnect()


def test_end_to_end(e2e: dict[str, Any]) -> None:
    pub: HAPublisher = e2e["pub"]
    sniffer: _Sniffer = e2e["sniffer"]
    hub = e2e["hub"]
    state_topic = pub._state_topic()
    cfg = f"homeassistant/sensor/x2d_{SERIAL}/nozzle_temp/config"

    assert sniffer.wait(lambda: cfg in sniffer.retained), "discovery not published"
    assert sniffer.wait(lambda: sniffer.retained.get(pub._availability_topic()) == b"online")
    assert sniffer.wait(
        lambda: sniffer.retained.get(pub._printer_availability_topic()) == b"online")
    # Legacy (name-based) config cleared.
    assert sniffer.wait(lambda: sniffer.retained.get(
        "homeassistant/sensor/x2d_studio/nozzle_temp/config") == b"")

    # Full report, then a delta: the retained state stays complete.
    hub.publish(_x2d_state())
    assert sniffer.wait(lambda: state_topic in sniffer.retained)
    hub.publish({"print": {"command": "push_status", "mc_percent": 42}})

    def _merged() -> bool:
        body = sniffer.retained.get(state_topic)
        if not body:
            return False
        pr = json.loads(body)["print"]
        return pr.get("mc_percent") == 42 and pr.get("gcode_state") == "FINISH" \
            and len(pr["ams"]["ams"]) == 4
    assert sniffer.wait(_merged), "delta overwrote the merged state"
    # AMS entities appeared once the state showed the units.
    assert sniffer.wait(lambda: f"homeassistant/sensor/x2d_{SERIAL}/ams_slot16_color/config"
                        in sniffer.retained)

    # HA birth message -> discovery republished.
    before = sniffer.count(cfg)
    e2e["seed"].publish("homeassistant/status", "online", qos=1)
    assert sniffer.wait(lambda: sniffer.count(cfg) > before), "no republish on HA birth"

    # Printer link goes stale -> printer_availability offline.
    e2e["last_ts"]["value"] = time.time() - 600
    assert sniffer.wait(
        lambda: sniffer.retained.get(pub._printer_availability_topic()) == b"offline")

    payload = json.loads(sniffer.retained[cfg])
    assert payload["default_entity_id"] == f"sensor.x2d_{SERIAL}_nozzle_temp"
    assert copy.deepcopy(payload["device"])["serial_number"] == SERIAL
