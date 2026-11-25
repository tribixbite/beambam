# Beambam Home Assistant native integration (HACS) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a HACS-installable Home Assistant custom integration that makes HA Core itself run beambam (no extra computer, no external MQTT broker), with full monitoring, signed start-print using the extracted RSA key, live camera stream, and nozzle+color mapping — plus close the same gaps in the existing MQTT-discovery bridge.

**Architecture:** A native integration `custom_components/beambam/` whose `manifest.json` requires `beambam>=1.6.0` from PyPI. A `DataUpdateCoordinator` wraps `beambam.mqtt.X2DClient` (LAN MQTT TLS:8883); pushall JSON flows to native HA entities. Signed control/print reuse `beambam.print_job.start_print` with new per-call signing-path params. Color mapping consumes `runtime.colorsync.mapper`; nozzle mapping uses a new `beambam.hwmap`. Three signing methods (paste/file/cloud) via config_flow. Layer B re-uses the same new beambam primitives in `runtime/ha/publisher.py` + `beambam/serve_http.py`.

**Tech Stack:** Python 3.10+ (HA Core), paho-mqtt, cryptography (both ship in HA), Home Assistant `DataUpdateCoordinator`/`ConfigFlow`/entity platforms, `pytest-homeassistant-custom-component`, in-process `amqtt` for bridge tests.

**Coordination note:** The other agent owns `beambam/filament_match.py`, `beambam/ams.py`, `x2d_slice.py`, `runtime/colorsync/*`, and `request_auto_nozzle_mapping` in `beambam/print_job.py`. This plan **consumes** those (never edits them). The only edit to `print_job.py` is the `_publish_signed_or_legacy` / `start_print` signing-path threading (lines 94-122 and 488-661), which do not overlap `request_auto_nozzle_mapping` (125-182). Commit C-phase quickly to minimize the shared-file window.

**Test runner note (Termux):** use `python3.12 -m pytest …`. `bun run check` is broken here; not relevant (Python project). Run throwaway scripts from a clean dir, never `$PREFIX/tmp` directly.

---

## File Structure

**Layer C — beambam core (v1.6.0):**
- Modify `beambam/print_job.py` — per-call signing paths on `_publish_signed_or_legacy` + `start_print`.
- Create `beambam/hwmap.py` — nozzle type/diameter presentation helpers.
- Modify `beambam/serve_http.py` — `/control/start_print` POST route.
- Modify `beambam/_version.py`, `pyproject.toml`, `CHANGELOG.md` — 1.6.0.
- Test `tests/test_signing_paths.py`, `tests/test_hwmap.py`, `tests/test_start_print_route.py`.

**Layer B — existing bridge gap-fix:**
- Modify `runtime/ha/publisher.py` — nozzle/color sensors + Start-print button + START dispatch.
- Test: extend `runtime/ha/test_ha.py` (or new `runtime/ha/test_startprint_bridge.py`).

**Layer A — native integration `custom_components/beambam/`:**
- `manifest.json`, `const.py`, `__init__.py`, `coordinator.py`, `entity.py`,
  `descriptions.py`, `sensor.py`, `binary_sensor.py`, `switch.py`, `light.py`,
  `button.py`, `number.py`, `camera.py`, `image.py`, `config_flow.py`,
  `services.yaml`, `strings.json`, `translations/en.json`, `diagnostics.py`.
- Test `tests/components/beambam/` (conftest + config_flow + init + entities).

**Layer P — packaging/docs/release:**
- `hacs.json` (repo root), `docs/HACS.md`, edits to `docs/HA.md` / `docs/HA_SETUP.md` (boo rename, signed print), `README.md` section, GitHub release for 1.6.0.

---

# PHASE C — beambam core primitives (v1.6.0)

### Task C1: Per-call signing-material paths in `print_job.py`

**Files:**
- Modify: `beambam/print_job.py:94-122` (`_publish_signed_or_legacy`)
- Modify: `beambam/print_job.py:488-594,661` (`start_print` signature + call sites)
- Test: `tests/test_signing_paths.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signing_paths.py
"""C1: start_print / _publish_signed_or_legacy honor per-call signing paths."""
from __future__ import annotations
import json
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from beambam import print_job


class _FakePaho:
    def __init__(self): self.published = []
    def publish(self, topic, payload, qos=1): self.published.append((topic, payload, qos))


class _FakeClient:
    """Mimics X2DClient surface used by the signing path."""
    def __init__(self, serial="03X2DTEST", with_raw=True):
        class _C: pass
        self.creds = _C(); self.creds.serial = serial
        self.client = _FakePaho() if with_raw else None
        self.hl_published = []
    def publish(self, payload, qos=1, **kw): self.hl_published.append(payload)


def _write_key(tmp: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp / "sign.pem"
    p.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return p


def test_publish_uses_injected_key_and_cert_id(tmp_path, monkeypatch):
    # No ~/.x2d defaults present → without injection it would fall back to
    # high-level publish; with injection it must take the signed-raw path.
    monkeypatch.setattr(print_job.Path, "home", staticmethod(lambda: tmp_path))
    key_path = _write_key(tmp_path)
    cli = _FakeClient()
    print_job._publish_signed_or_legacy(
        cli, {"print": {"command": "stop", "sequence_id": "1"}},
        sign_key_path=key_path, cert_id="UNITTESTCERTID")
    # Signed-raw path publishes to device/<serial>/request on the paho client.
    assert cli.client.published, "expected a signed raw publish"
    topic, wire, qos = cli.client.published[0]
    assert topic == f"device/{cli.creds.serial}/request"
    assert b"UNITTESTCERTID" in wire  # cert_id appears in the signed header
    assert not cli.hl_published, "must not fall back to high-level publish"


def test_publish_without_injection_and_no_defaults_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(print_job.Path, "home", staticmethod(lambda: tmp_path))
    cli = _FakeClient()
    print_job._publish_signed_or_legacy(cli, {"print": {"command": "stop"}})
    assert cli.hl_published and not cli.client.published
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_signing_paths.py -v`
Expected: FAIL — `_publish_signed_or_legacy() got an unexpected keyword argument 'sign_key_path'`.

- [ ] **Step 3: Edit `_publish_signed_or_legacy` (lines 94-122)**

Replace the function header + path resolution. New full function:

```python
def _publish_signed_or_legacy(client: "X2DClient", payload: dict, *,
                              sign_key_path: "Path | None" = None,
                              cert_id: "str | None" = None) -> None:
    """Publish a `print.*` payload with the right signature for this firmware.

    Jan-2025+ X2D/H2D firmware verifies `print.*` against a Bambu-CA-issued cert.
    If the recovered per-install key is present (`~/.x2d/printer_sign_key.pem` +
    `printer_cert_id.txt`, or the per-call `sign_key_path`/`cert_id` overrides
    used by the Home Assistant integration when one process drives multiple
    printers) we sign the command with it (`mqtt_sign`) and publish the raw
    wire — the only signature the printer accepts for print control. The legacy
    `X2DClient.publish` path signs with the leaked Bambu Connect cert, which the
    firmware rejects for `print.*` (err 84033545). Falls back to it only when no
    recovered key is available (older firmware)."""
    key_path = sign_key_path or (Path.home() / ".x2d" / "printer_sign_key.pem")
    cid_path = Path.home() / ".x2d" / "printer_cert_id.txt"
    resolved_cert_id = cert_id or (
        cid_path.read_text().strip() if cid_path.is_file() else "")
    # Take the signed-raw path only when a recovered key + cert_id are present
    # AND the client exposes a real paho `.client` (raw publish). Test/simulate
    # clients capture via their high-level `publish()` and have no `.client`.
    if (not (key_path.is_file() and resolved_cert_id)
            or getattr(client, "client", None) is None):
        client.publish(payload)
        return
    from beambam import mqtt_sign
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(key_path.read_bytes(), None)
    family = next(iter(payload))                            # "print"
    command = payload[family]                               # already has seq+ts
    wire = mqtt_sign.signed_message(family, command, _signing_user_id(),
                                    signer=mqtt_sign.key_signer(key),
                                    cert_id=resolved_cert_id)
    topic = f"device/{client.creds.serial}/request"
    client.client.publish(topic, wire, qos=1)
```

- [ ] **Step 4: Thread params through `start_print` (lines 488-594, 661)**

Edit the signature (after `local_path: Path | None = None`) to add three kwargs:

```python
def start_print(client: "X2DClient", gcode_filename: str, *,
                use_ams: bool = True,
                ams_slot: int | list[int] = 0,
                bed_levelling: bool = True, flow_cali: bool = False,
                timelapse: bool = False, vibration_cali: bool = False,
                bed_type: str | None = None,
                bed_temp: int | None = None,
                local_path: Path | None = None,
                sign_key_path: "Path | None" = None,
                cert_id: "str | None" = None,
                device_cert_path: "Path | None" = None) -> None:
```

Change the device-cert detection block (currently lines 589-594) to honor the override:

```python
    _dev_cert = device_cert_path or _DEVICE_CERT_PATH
    if _dev_cert.is_file():
        x2d_body = build_x2d_project_file(
            gcode_filename, md5_hex, ams_slots=ams_mapping_legacy,
            bed_type=bed_type, use_ams=use_ams, timelapse=timelapse,
            device_cert_path=device_cert_path)
        _publish_signed_or_legacy(client, {"print": x2d_body},
                                  sign_key_path=sign_key_path, cert_id=cert_id)
        return
```

Change the final legacy publish (line 661) to:

```python
    _publish_signed_or_legacy(client, payload,
                              sign_key_path=sign_key_path, cert_id=cert_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_signing_paths.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Regression — existing print_job tests still pass**

Run: `python3.12 -m pytest tests/ -k "print or sign or start" -q`
Expected: PASS (no regressions). If a test imports `Path` differently, note it but do not change unrelated tests.

- [ ] **Step 7: Commit**

```bash
git add beambam/print_job.py tests/test_signing_paths.py
git commit -m "feat(print): per-call signing-key/cert/device-cert paths for start_print

Lets one process (HA integration) sign for multiple printers without a shared
~/.x2d. Defaults unchanged. — Opus 4.8"
```

---

### Task C2: `beambam/hwmap.py` — nozzle type/diameter presentation

**Files:**
- Create: `beambam/hwmap.py`
- Test: `tests/test_hwmap.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hwmap.py
from beambam import hwmap


def test_nozzle_type_name_known():
    assert hwmap.nozzle_type_name("hardened_steel") == "Hardened Steel"
    assert hwmap.nozzle_type_name("stainless_steel") == "Stainless Steel"


def test_nozzle_type_name_unknown_titlecases():
    assert hwmap.nozzle_type_name("tungsten_carbide") == "Tungsten Carbide"
    assert hwmap.nozzle_type_name("") == ""
    assert hwmap.nozzle_type_name(None) == ""


def test_nozzle_diameter_mm_parses():
    assert hwmap.nozzle_diameter_mm("0.4") == 0.4
    assert hwmap.nozzle_diameter_mm(0.6) == 0.6
    assert hwmap.nozzle_diameter_mm("") is None
    assert hwmap.nozzle_diameter_mm("garbage") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_hwmap.py -v`
Expected: FAIL — `No module named 'beambam.hwmap'`.

- [ ] **Step 3: Create `beambam/hwmap.py`**

```python
"""Hardware presentation helpers — map raw firmware codes to human labels.

Color/filament mapping lives in ``runtime.colorsync.mapper`` (item #58); this
module covers the gap it doesn't: nozzle type + diameter. Pure data, no I/O, so
both the Home Assistant native integration and the MQTT-discovery bridge import
it without side effects.
"""
from __future__ import annotations

# Bambu's pushall reports `print.nozzle_type` as a lowercase word form
# (verified in repo captures: "hardened_steel", "stainless_steel"). Older docs
# referenced short codes ("HS01"); map both, then fall back to title-casing so
# an unseen value still renders readably instead of as a raw token.
_NOZZLE_TYPE_NAMES: dict[str, str] = {
    "hardened_steel": "Hardened Steel",
    "stainless_steel": "Stainless Steel",
    "hs01": "Hardened Steel",
    "ss01": "Stainless Steel",
}


def nozzle_type_name(code: str | None) -> str:
    """Human-readable nozzle type. Unknown non-empty values are title-cased
    (underscores → spaces); empty/None → ''."""
    if not code:
        return ""
    key = str(code).strip().lower()
    if key in _NOZZLE_TYPE_NAMES:
        return _NOZZLE_TYPE_NAMES[key]
    return str(code).strip().replace("_", " ").title()


def nozzle_diameter_mm(raw: str | float | None) -> float | None:
    """Normalize the firmware's string diameter ('0.4') to a float, or None
    when absent/unparseable."""
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_hwmap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add beambam/hwmap.py tests/test_hwmap.py
git commit -m "feat(hwmap): nozzle type/diameter presentation helpers — Opus 4.8"
```

---

### Task C3: `/control/start_print` HTTP route in `serve_http.py`

**Files:**
- Modify: `beambam/serve_http.py` — add `start_print` branch in `do_POST` control dispatch (after the `sound` branch, before the `else` at ~line 1006); extend the `supported` list.
- Test: `tests/test_start_print_route.py`

The control dispatch ends with `_publish_via_client`, but `start_print` needs the live `X2DClient` (closure var `clients`), not a prebuilt payload. Handle it as its own branch that returns directly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_start_print_route.py
"""C3: POST /control/start_print invokes beambam.print_job.start_print with the
live client + parsed body, and returns JSON."""
from __future__ import annotations
import json
import threading
import urllib.request
from contextlib import closing
import socket
import pytest
from beambam.serve_http import _serve_http


class _FakeClient:
    def __init__(self): self.creds = type("C", (), {"serial": "03X2DTEST"})()
    def publish(self, *a, **k): pass


def _free_port():
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def test_start_print_route(monkeypatch):
    calls = {}
    import beambam.print_job as pj

    def fake_start_print(client, gcode, **kw):
        calls["client"] = client; calls["gcode"] = gcode; calls["kw"] = kw

    monkeypatch.setattr(pj, "start_print", fake_start_print)
    cli = _FakeClient()
    port = _free_port()
    t = threading.Thread(
        target=_serve_http,
        kwargs={"bind": f"127.0.0.1:{port}",
                "get_state": lambda p: {"print": {}},
                "get_last_ts": lambda p: 0.0,
                "printer_names": ["studio"],
                "clients": {"studio": cli}},
        daemon=True)
    t.start()
    # crude readiness wait
    import time; time.sleep(0.6)
    body = json.dumps({"gcode": "x.gcode.3mf", "slot": 2,
                       "use_ams": True}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/control/start_print?printer=studio",
        data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        resp = json.loads(r.read())
    assert resp.get("ok") is True
    assert calls["gcode"] == "x.gcode.3mf"
    assert calls["kw"]["ams_slot"] == 2
    assert calls["client"] is cli
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_start_print_route.py -v`
Expected: FAIL — route returns 404 (`unknown control verb 'start_print'`).

- [ ] **Step 3: Add the `start_print` branch**

In `beambam/serve_http.py`, inside `do_POST`, locate the `elif verb == "sound":` block (~line 994). Immediately **before** the final `else:` (`unknown control verb`, ~line 1006) insert:

```python
            elif verb == "start_print":
                # Signed start-print of a file already on the printer (or one
                # uploaded out-of-band). Needs the LIVE client, not a prebuilt
                # payload, so it returns here instead of falling through to
                # _publish_via_client.
                cli = (clients or {}).get(printer)
                if cli is None:
                    self._send_json({"error":
                        f"no live MQTT client for printer {printer!r}"},
                        status=503); return
                gcode = (body or {}).get("gcode", "")
                if not isinstance(gcode, str) or not gcode.strip():
                    self._send_json({"error":
                        "expected {gcode, slot?, use_ams?, local_path?, "
                        "bed_type?, bed_temp?, flow_cali?, timelapse?, "
                        "vib_cali?, bed_levelling?}"}, status=400); return
                slot_raw = body.get("slot", 0)
                try:
                    slot = (int(slot_raw) if not isinstance(slot_raw, list)
                            else [int(s) for s in slot_raw])
                except (TypeError, ValueError):
                    self._send_json({"error": "slot must be int or int list"},
                                    status=400); return
                lp = body.get("local_path")
                local_path = Path(lp) if lp else None
                from beambam.print_job import start_print, PrintRefusal
                try:
                    start_print(
                        cli, gcode,
                        use_ams=bool(body.get("use_ams", True)),
                        ams_slot=slot,
                        bed_levelling=bool(body.get("bed_levelling", True)),
                        flow_cali=bool(body.get("flow_cali", False)),
                        timelapse=bool(body.get("timelapse", False)),
                        vibration_cali=bool(body.get("vib_cali", False)),
                        bed_type=body.get("bed_type"),
                        bed_temp=(int(body["bed_temp"])
                                  if body.get("bed_temp") is not None else None),
                        local_path=(local_path
                                    if local_path and local_path.is_file()
                                    else None))
                except PrintRefusal as e:
                    self._send_json({"error": f"refused: {e}"}, status=409)
                    return
                except Exception as e:  # noqa: BLE001
                    self._send_json({"error": f"start_print failed: {e}"},
                                    status=502); return
                self._send_json({"ok": True, "printer": printer,
                                 "gcode": gcode}); return
```

Also extend the `supported` list in the trailing `else` to include `"start_print"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.12 -m pytest tests/test_start_print_route.py -v`
Expected: PASS.

- [ ] **Step 5: Confirm `PrintRefusal` is importable**

Run: `python3.12 -c "from beambam.print_job import PrintRefusal; print('ok')"`
Expected: `ok`. (If the symbol differs, grep `class .*Refusal` in `print_job.py` and use the real name.)

- [ ] **Step 6: Commit**

```bash
git add beambam/serve_http.py tests/test_start_print_route.py
git commit -m "feat(serve): POST /control/start_print — signed start-print over the daemon HTTP API — Opus 4.8"
```

---

### Task C4: Version bump 1.5.1 → 1.6.0

**Files:**
- Modify: `beambam/_version.py`, `pyproject.toml:7`, `CHANGELOG.md`

- [ ] **Step 1: Find the version string**

Run: `python3.12 -c "import beambam._version as v; print(v.__dict__)"` and `command grep -n version beambam/_version.py pyproject.toml`

- [ ] **Step 2: Bump both to `1.6.0`** (match the exact existing format in `_version.py`).

- [ ] **Step 3: Add CHANGELOG entry** at the top:

```markdown
## 1.6.0 — 2026-06-23

### Added
- **Native Home Assistant integration** (`custom_components/beambam/`, HACS-installable): HA Core runs beambam directly — no separate host, no external MQTT broker. config_flow with three signing methods (paste PEM / file path / Bambu cloud login), live camera stream, and signed start-print using the extracted RSA key.
- `POST /control/start_print` on the bridge daemon HTTP API (signed start-print).
- `beambam.hwmap` — nozzle type/diameter presentation helpers.
- HA discovery bridge (`beambam ha-publish`) now exposes a Start-print button and nozzle/color-mapping sensors.

### Changed
- `start_print()` / `_publish_signed_or_legacy()` accept per-call signing-key / cert-id / device-cert paths (multi-printer in one process).
- Docs reference `beambam boo` (canonical) over the `daemon` alias.
```

- [ ] **Step 4: Verify import + version**

Run: `python3.12 -c "import beambam; print(beambam.__version__ if hasattr(beambam,'__version__') else 'see _version')"`
Expected: shows `1.6.0` (or `_version` exposes it).

- [ ] **Step 5: Commit**

```bash
git add beambam/_version.py pyproject.toml CHANGELOG.md
git commit -m "chore(release): bump to 1.6.0 (HA native integration) — Opus 4.8"
```

---

# PHASE B — existing MQTT-discovery bridge gap-fix

### Task B1: Nozzle/color sensors + Start-print button in `runtime/ha/publisher.py`

**Files:**
- Modify: `runtime/ha/publisher.py` — add entities; add a `_colorsync_loop` that republishes per-slot filament names; add START dispatch.
- Test: `runtime/ha/test_startprint_bridge.py`

The publisher pushes the raw pushall to `x2d/<id>/state` and entities project it with `value_template`. Color *name* mapping needs the catalog (not pure Jinja), so publish a companion JSON to `x2d/<id>/colorsync` and add sensors that template off it.

- [ ] **Step 1: Add nozzle + colorsync entities**

In `runtime/ha/publisher.py`, in `ams_entities()` (after the slot color/material loop, before the humidity-warn loop) add per-slot filament-name sensors that read a companion topic:

```python
        out.append(Entity(
            "sensor", f"ams_slot{slot}_filament", f"AMS slot {slot} filament",
            "{{ value_json.slots['" + str(slot) + "'] | default('') }}",
            "", "", "", "mdi:palette-swatch"))
```

But these read a DIFFERENT state topic. Simpler + DRY: give them their own
`state_topic` via `extra`. Update `_discovery_payload` so an entity may override
`state_topic` from `extra` (it already merges `extra`; ensure `extra`'s
`state_topic` wins). Set:

```python
            extra={"state_topic": "__BASE__/colorsync"}))
```

Add a nozzle-type sensor to `SENSOR_ENTITIES` (the `nozzle_type` entry already
exists at line 168 but shows the raw code). Add a readable one:

```python
    Entity("sensor", "nozzle_type_name", "Nozzle type (readable)",
           "{{ value_json.print.nozzle_type | default('') | replace('_',' ') | title }}",
           "", "", "", "mdi:printer-3d-nozzle-alert"),
```

(Title-casing in Jinja matches `beambam.hwmap.nozzle_type_name` for the known
word-form values; the daemon-side `hwmap` is used by the native integration.)

- [ ] **Step 2: Add a Start-print button + colorsync publisher**

Add to `CONTROL_ENTITIES`:

```python
    Entity("button", "start_print", "Start print",
           "", "", "", "", "mdi:printer-3d",
           extra={"command_topic": "__BASE__/print/set",
                  "payload_press": "START"}),
```

In `HAPublisher.__init__`, add `self._colorsync_thread = None`. Add a method:

```python
    def _colorsync_loop(self) -> None:
        """Republish per-slot filament NAME (catalog match) to
        x2d/<id>/colorsync as {"slots": {"1": "Bambu PLA Basic Orange", ...}}.
        Reads the same SSE state the publisher already consumes, so no extra
        printer load. Best-effort: if the catalog module is unavailable the
        loop exits quietly and the name sensors stay blank."""
        try:
            from runtime.colorsync.mapper import state_for
        except Exception:  # noqa: BLE001
            return
        url = (f"{self.daemon_url}/state"
               f"?printer={urllib.parse.quote(self.printer_name)}")
        while not self._stop.is_set():
            try:
                req = urllib.request.Request(url)
                if self.daemon_token:
                    req.add_header("Authorization", f"Bearer {self.daemon_token}")
                with urllib.request.urlopen(req, timeout=8) as r:
                    state = json.loads(r.read() or b"{}")
                rows = state_for(state.get("state") or state,
                                 model=self.device_model)
                slots = {str(row["slot"]):
                         ((row.get("match") or {}).get("profile") or "")
                         for row in rows}
                self._client.publish(f"{self.base_topic}/colorsync",
                                     json.dumps({"slots": slots}),
                                     qos=0, retain=True)
            except Exception as e:  # noqa: BLE001
                LOG.debug("colorsync loop: %s", e)
            if self._stop.wait(15.0):
                break
```

Start it in `start()` alongside the snapshot thread:

```python
        self._colorsync_thread = threading.Thread(
            target=self._colorsync_loop,
            name=f"x2d-ha-colorsync-{self.printer_name}", daemon=True)
        self._colorsync_thread.start()
```

- [ ] **Step 3: Add START dispatch in `_on_message`**

In `_on_message`, in the `print/set` branch (~line 522), add `START` handling:

```python
            elif topic == f"{self.base_topic}/print/set":
                v = payload.upper()
                if v in ("PAUSE", "RESUME", "STOP"):
                    self._http_post("/control/" + v.lower(), {})
                elif v == "START":
                    # Start the configured default file/slot. Names come from
                    # env so the discovery bridge stays declarative.
                    gcode = os.environ.get("X2D_HA_START_FILE", "")
                    if gcode:
                        self._http_post("/control/start_print",
                                        {"gcode": gcode,
                                         "slot": int(os.environ.get(
                                             "X2D_HA_START_SLOT", "0"))})
                    else:
                        LOG.warning("START pressed but X2D_HA_START_FILE unset")
                elif v == "HOME":
                    self._http_post("/control/gcode", {"line": "G28"})
                elif v == "LEVEL":
                    self._http_post("/control/gcode", {"line": "G29"})
```

- [ ] **Step 4: Write the test**

```python
# runtime/ha/test_startprint_bridge.py
"""B1: START payload on print/set forwards to /control/start_print."""
import os
from runtime.ha.publisher import HAPublisher


def test_start_dispatch(monkeypatch):
    pub = HAPublisher(broker_host="127.0.0.1", daemon_url="http://x",
                      printer_name="studio", device_serial="03X2DTEST")
    posts = []
    monkeypatch.setattr(pub, "_http_post", lambda path, body: posts.append((path, body)))
    monkeypatch.setenv("X2D_HA_START_FILE", "demo.gcode.3mf")
    monkeypatch.setenv("X2D_HA_START_SLOT", "3")

    class _Msg:
        topic = f"{pub.base_topic}/print/set"
        payload = b"START"
    pub._on_message(None, None, _Msg())
    assert posts == [("/control/start_print",
                      {"gcode": "demo.gcode.3mf", "slot": 3})]


def test_start_dispatch_no_file(monkeypatch):
    pub = HAPublisher(broker_host="127.0.0.1", daemon_url="http://x",
                      printer_name="studio", device_serial="03X2DTEST")
    posts = []
    monkeypatch.setattr(pub, "_http_post", lambda p, b: posts.append((p, b)))
    monkeypatch.delenv("X2D_HA_START_FILE", raising=False)

    class _Msg:
        topic = f"{pub.base_topic}/print/set"
        payload = b"START"
    pub._on_message(None, None, _Msg())
    assert posts == []  # no file configured → no forward
```

- [ ] **Step 5: Run the test**

Run: `python3.12 -m pytest runtime/ha/test_startprint_bridge.py -v`
Expected: PASS (both).

- [ ] **Step 6: Confirm discovery still builds with the new entities**

Run:
```bash
python3.12 -c "
from runtime.ha.publisher import HAPublisher
p=HAPublisher(broker_host='127.0.0.1',daemon_url='http://x',printer_name='studio',device_serial='03X2DTEST')
keys=[e.key for e in p._entities]
assert 'start_print' in keys and 'nozzle_type_name' in keys
assert any(k.endswith('_filament') for k in keys)
print('entities ok:', len(keys))
"
```
Expected: `entities ok: <n>`.

- [ ] **Step 7: Commit**

```bash
git add runtime/ha/publisher.py runtime/ha/test_startprint_bridge.py
git commit -m "feat(ha-bridge): Start-print button, START dispatch, nozzle/color-name sensors — Opus 4.8"
```

---

### Task B2: End-to-end bridge discovery smoke (amqtt)

**Files:**
- Test: extend `runtime/ha/test_ha.py` OR new `runtime/ha/test_bridge_e2e.py`

- [ ] **Step 1: Inspect the existing harness**

Run: `command grep -nE "amqtt|Broker|HAPublisher|subscribe|homeassistant/" runtime/ha/test_ha.py | head`
Use the same broker-spinup pattern already present.

- [ ] **Step 2: Add an assertion** that, after `start()`, a discovery config for
`homeassistant/button/x2d_<serial>/start_print/config` is retained on the broker,
and that publishing `START` to `x2d/<serial>/print/set` triggers a
`/control/start_print` POST (point `daemon_url` at a tiny stub HTTP server that
records the path). Follow the existing test's structure exactly.

- [ ] **Step 3: Run**

Run: `python3.12 -m pytest runtime/ha/test_ha.py runtime/ha/test_bridge_e2e.py -v`
Expected: PASS. (If `amqtt` missing: `python3.12 -m pip install amqtt`.)

- [ ] **Step 4: Commit**

```bash
git add runtime/ha/test_*.py
git commit -m "test(ha-bridge): e2e start_print discovery + dispatch over amqtt — Opus 4.8"
```

---

# PHASE A — native HACS integration `custom_components/beambam/`

> Tests use `pytest-homeassistant-custom-component`. Install once:
> `python3.12 -m pip install pytest-homeassistant-custom-component`. If it cannot
> install on Termux, mark Phase-A tests `@pytest.mark.skipif` on import error and
> record that in the verification report (Task P4); still author the tests.

### Task A0: Scaffold + `manifest.json` + `const.py` + `hacs.json`

**Files:**
- Create: `custom_components/beambam/__init__.py` (minimal for now),
  `custom_components/beambam/manifest.json`, `custom_components/beambam/const.py`,
  `hacs.json` (repo root).

- [ ] **Step 1: `const.py`**

```python
"""Constants for the beambam Home Assistant integration."""
from __future__ import annotations

DOMAIN = "beambam"
MANUFACTURER = "Bambu Lab"

PLATFORMS: list[str] = [
    "sensor", "binary_sensor", "switch", "light",
    "button", "number", "camera", "image",
]

# config-entry data / options keys
CONF_HOST = "host"
CONF_ACCESS_CODE = "access_code"
CONF_SERIAL = "serial"
CONF_MODEL = "model"
CONF_SIGNING_METHOD = "signing_method"
CONF_SIGN_KEY_PEM = "sign_key_pem"
CONF_CERT_ID = "cert_id"
CONF_DEVICE_CERT_PEM = "device_cert_pem"
CONF_SIGN_KEY_PATH = "sign_key_path"
CONF_DEVICE_CERT_PATH = "device_cert_path"
CONF_CERT_ID_PATH = "cert_id_path"
CONF_CLOUD_EMAIL = "cloud_email"
CONF_CLOUD_PASSWORD = "cloud_password"
CONF_CAMERA_URL = "camera_url"
CONF_RTSP_PORT = "rtsp_port"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_START_FILE = "start_file"
CONF_START_SLOT = "start_slot"

# signing method values
SIGNING_NONE = "none"
SIGNING_PASTE = "paste"
SIGNING_FILE = "file"
SIGNING_CLOUD = "cloud"

DEFAULT_MODEL = "X2D"
DEFAULT_RTSP_PORT = 322
DEFAULT_SCAN_INTERVAL = 5

# supported models for the picker
MODELS = ["X1C", "X1E", "P1S", "P1P", "A1", "A1 mini", "H2D", "H2S", "X2D"]
# proprietary chamber-image models (no RTSPS) → use lvl_local pump
PROPRIETARY_CAM_MODELS = {"P1S", "P1P", "A1", "A1 mini"}
```

- [ ] **Step 2: `manifest.json`**

```json
{
  "domain": "beambam",
  "name": "Beambam (Bambu Lab)",
  "version": "1.6.0",
  "documentation": "https://github.com/tribixbite/beambam/blob/main/docs/HACS.md",
  "issue_tracker": "https://github.com/tribixbite/beambam/issues",
  "codeowners": ["@tribixbite"],
  "config_flow": true,
  "iot_class": "local_push",
  "integration_type": "device",
  "requirements": ["beambam>=1.6.0"],
  "zeroconf": ["_bambulab._tcp.local."],
  "ssdp": [{ "manufacturer": "Bambu Lab" }]
}
```

> NOTE: verify the real Bambu zeroconf service type during Task A12 against a
> live printer if available; `_bambulab._tcp.local.` is the documented type used
> by ha-bambulab. If unknown, keep the key (harmless) and rely on manual add.

- [ ] **Step 3: `hacs.json` (repo root)**

```json
{
  "name": "Beambam (Bambu Lab)",
  "homeassistant": "2024.1.0",
  "render_readme": true,
  "zip_release": false,
  "content_in_root": false
}
```

(`content_in_root: false` → HACS finds the integration at `custom_components/beambam/`.)

- [ ] **Step 4: Minimal `__init__.py`** (replaced fully in A10):

```python
"""The beambam integration (scaffold; completed in later tasks)."""
```

- [ ] **Step 5: Validate JSON**

Run: `python3.12 -c "import json; [json.load(open(p)) for p in ['custom_components/beambam/manifest.json','hacs.json']]; print('json ok')"`
Expected: `json ok`.

- [ ] **Step 6: Commit**

```bash
git add custom_components/beambam/__init__.py custom_components/beambam/manifest.json custom_components/beambam/const.py hacs.json
git commit -m "feat(hacs): scaffold custom_components/beambam + hacs.json + manifest — Opus 4.8"
```

---

### Task A1: `coordinator.py`

**Files:**
- Create: `custom_components/beambam/coordinator.py`
- Test: `tests/components/beambam/test_coordinator.py`

- [ ] **Step 1: Implement the coordinator**

```python
"""Data coordinator: wraps beambam.mqtt.X2DClient and pushes pushall JSON."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACCESS_CODE, CONF_CERT_ID, CONF_HOST, CONF_MODEL, CONF_SCAN_INTERVAL,
    CONF_SERIAL, CONF_SIGNING_METHOD, DEFAULT_MODEL, DEFAULT_SCAN_INTERVAL,
    DOMAIN, SIGNING_CLOUD,
)

_LOGGER = logging.getLogger(__name__)


class BeambamCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One per config entry / printer. Owns the MQTT client lifecycle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.serial: str = entry.data[CONF_SERIAL]
        self.access_code: str = entry.data[CONF_ACCESS_CODE]
        self.model: str = entry.data.get(CONF_MODEL, DEFAULT_MODEL)
        self.signing_method: str = entry.data.get(CONF_SIGNING_METHOD, "none")
        # per-entry signing material dir (written in __init__.py setup)
        self.signing_dir: Path = Path(
            hass.config.path(".storage", DOMAIN, entry.entry_id))
        self.cert_id: str | None = entry.data.get(CONF_CERT_ID) or None
        self._client = None  # beambam.mqtt.X2DClient
        interval = entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_{self.serial}",
            update_interval=timedelta(seconds=int(interval)))

    # ----- lifecycle ---------------------------------------------------
    async def async_connect(self) -> None:
        """Connect the MQTT client (executor) and wire the state callback."""
        from beambam.config import Creds
        from beambam.mqtt import X2DClient
        creds = Creds(ip=self.host, code=self.access_code,
                      serial=self.serial, name=self.entry.title)

        @callback
        def _push(state: dict) -> None:
            self.async_set_updated_data(state)

        def _on_state(state: dict) -> None:
            # paho thread → hop to the loop thread safely
            self.hass.loop.call_soon_threadsafe(_push, state)

        self._client = X2DClient(creds, on_state=_on_state)
        await self.hass.async_add_executor_job(self._client.connect)
        # prime an initial pushall
        await self.hass.async_add_executor_job(self._pushall)

    async def async_shutdown(self) -> None:
        if self._client is not None:
            await self.hass.async_add_executor_job(self._client.disconnect)
            self._client = None

    def _pushall(self) -> None:
        from beambam.cli._helpers import _next_seq
        if self._client is not None:
            self._client.publish(
                {"pushing": {"sequence_id": _next_seq(), "command": "pushall"}})

    async def _async_update_data(self) -> dict[str, Any]:
        """Periodic liveness: ask for a fresh pushall; entities update via the
        push callback. Returns the last known state so availability tracks."""
        if self._client is None:
            await self.async_connect()
        await self.hass.async_add_executor_job(self._pushall)
        return self.data or {}

    # ----- control helpers (executor-wrapped) --------------------------
    async def async_publish(self, payload: dict) -> None:
        await self.hass.async_add_executor_job(self._client.publish, payload)

    def _signing_paths(self) -> dict:
        """Resolve per-entry signing kwargs for start_print."""
        if self.signing_method == SIGNING_CLOUD:
            return {}  # cloud routing handled separately
        kp = self.signing_dir / "printer_sign_key.pem"
        dc = self.signing_dir / "printer_device_cert.pem"
        out: dict = {}
        if kp.is_file():
            out["sign_key_path"] = kp
        if self.cert_id:
            out["cert_id"] = self.cert_id
        if dc.is_file():
            out["device_cert_path"] = dc
        return out

    async def async_start_print(self, gcode: str, *, slot=0, use_ams=True,
                                local_path: str | None = None,
                                bed_type=None, bed_temp=None,
                                flow_cali=False, timelapse=False,
                                bed_levelling=True) -> None:
        from beambam.print_job import start_print
        kw = self._signing_paths()
        lp = Path(local_path) if local_path else None

        def _go() -> None:
            start_print(self._client, gcode, use_ams=use_ams, ams_slot=slot,
                        bed_levelling=bed_levelling, flow_cali=flow_cali,
                        timelapse=timelapse, bed_type=bed_type,
                        bed_temp=bed_temp,
                        local_path=lp if (lp and lp.is_file()) else None, **kw)
        await self.hass.async_add_executor_job(_go)
```

- [ ] **Step 2: Test (logic-level, no real broker)**

```python
# tests/components/beambam/test_coordinator.py
import pytest
pytest.importorskip("pytest_homeassistant_custom_component")
from custom_components.beambam.coordinator import BeambamCoordinator
from custom_components.beambam.const import (
    CONF_HOST, CONF_ACCESS_CODE, CONF_SERIAL, CONF_MODEL)


async def test_signing_paths_resolution(hass, tmp_path, monkeypatch):
    from homeassistant.config_entries import ConfigEntry
    entry = type("E", (), {})()
    entry.data = {CONF_HOST: "1.2.3.4", CONF_ACCESS_CODE: "x",
                  CONF_SERIAL: "03X2DTEST", CONF_MODEL: "X2D",
                  "signing_method": "paste", "cert_id": "CID"}
    entry.options = {}
    entry.entry_id = "abc"
    entry.title = "studio"
    coord = BeambamCoordinator(hass, entry)
    coord.signing_dir = tmp_path
    (tmp_path / "printer_sign_key.pem").write_text("k")
    (tmp_path / "printer_device_cert.pem").write_text("c")
    kw = coord._signing_paths()
    assert kw["cert_id"] == "CID"
    assert kw["sign_key_path"].name == "printer_sign_key.pem"
    assert kw["device_cert_path"].name == "printer_device_cert.pem"
```

- [ ] **Step 3: Run**

Run: `python3.12 -m pytest tests/components/beambam/test_coordinator.py -v`
Expected: PASS (or SKIPPED if the HA test harness can't install — record it).

- [ ] **Step 4: Commit**

```bash
git add custom_components/beambam/coordinator.py tests/components/beambam/test_coordinator.py
git commit -m "feat(hacs): MQTT coordinator wrapping X2DClient — Opus 4.8"
```

---

### Task A2: Entity base + description tables (`entity.py`, `descriptions.py`)

**Files:**
- Create: `custom_components/beambam/entity.py`, `custom_components/beambam/descriptions.py`

- [ ] **Step 1: `entity.py` (base + safe-dig helper)**

```python
"""Shared entity base + pushall navigation helper."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import BeambamCoordinator


def dig(data: Any, *path: str, default: Any = None) -> Any:
    """Safe nested lookup over pushall dict/list; '0','1' index into lists."""
    cur = data
    for key in path:
        if cur is None:
            return default
        if isinstance(cur, list):
            try:
                cur = cur[int(key)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            cur = cur.get(key, default if key == path[-1] else None)
        else:
            return default
    return cur if cur is not None else default


class BeambamEntity(CoordinatorEntity[BeambamCoordinator]):
    """Common device_info + availability."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BeambamCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{coordinator.serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.serial)},
            manufacturer=MANUFACTURER,
            model=c.model,
            name=f"{MANUFACTURER} {c.model}",
            serial_number=c.serial,
            sw_version=str(dig(c.data, "print", "upgrade_state",
                               "cur_state_code", default="")) or None,
        )

    @property
    def available(self) -> bool:
        return (super().available
                and bool(self.coordinator.data)
                and dig(self.coordinator.data, "print") is not None)
```

- [ ] **Step 2: `descriptions.py` (data-driven entity tables)**

Define description dataclasses with a `value_fn(coordinator.data) -> state`.
Mirror `runtime/ha/publisher.py` entity catalogue but as Python lambdas. Full
content:

```python
"""Declarative entity tables. value_fn reads the pushall dict via entity.dig."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from homeassistant.components.sensor import SensorEntityDescription
from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.components.number import NumberEntityDescription
from homeassistant.const import (
    PERCENTAGE, UnitOfTemperature, UnitOfTime, UnitOfLength, UnitOfMass,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
)

from .entity import dig
from beambam import hwmap


@dataclass(frozen=True, kw_only=True)
class BeambamSensorDesc(SensorEntityDescription):
    value_fn: Callable[[dict], Any] = lambda d: None


@dataclass(frozen=True, kw_only=True)
class BeambamBinaryDesc(BinarySensorEntityDescription):
    value_fn: Callable[[dict], bool] = lambda d: False


@dataclass(frozen=True, kw_only=True)
class BeambamNumberDesc(NumberEntityDescription):
    value_fn: Callable[[dict], Any] = lambda d: None
    # command target: ("bed"|"nozzle"|"chamber")
    target: str = ""


def _f(d, *p, default=None, cast=float, rnd=None):
    v = dig(d, *p, default=None)
    if v in (None, ""):
        return default
    try:
        v = cast(v)
    except (TypeError, ValueError):
        return default
    return round(v, rnd) if (rnd is not None and isinstance(v, float)) else v


SENSORS: list[BeambamSensorDesc] = [
    BeambamSensorDesc(key="nozzle_temp", name="Nozzle temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class="temperature", state_class="measurement",
        value_fn=lambda d: _f(d, "print", "nozzle_temper", cast=float, rnd=1)),
    BeambamSensorDesc(key="nozzle_target", name="Nozzle target",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class="temperature",
        value_fn=lambda d: _f(d, "print", "nozzle_target_temper", cast=float, rnd=0)),
    BeambamSensorDesc(key="bed_temp", name="Bed temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class="temperature", state_class="measurement",
        value_fn=lambda d: _f(d, "print", "bed_temper", cast=float, rnd=1)),
    BeambamSensorDesc(key="bed_target", name="Bed target",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class="temperature",
        value_fn=lambda d: _f(d, "print", "bed_target_temper", cast=float, rnd=0)),
    BeambamSensorDesc(key="chamber_temp", name="Chamber temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class="temperature", state_class="measurement",
        value_fn=lambda d: _f(d, "print", "chamber_temper", cast=float, rnd=1)),
    BeambamSensorDesc(key="progress", name="Print progress",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:progress-clock",
        state_class="measurement",
        value_fn=lambda d: _f(d, "print", "mc_percent", cast=int)),
    BeambamSensorDesc(key="current_layer", name="Current layer",
        icon="mdi:layers",
        value_fn=lambda d: _f(d, "print", "mc_current_layer", cast=int)),
    BeambamSensorDesc(key="total_layers", name="Total layers",
        icon="mdi:layers-outline",
        value_fn=lambda d: _f(d, "print", "total_layer_num", cast=int)),
    BeambamSensorDesc(key="remaining", name="Time remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES, device_class="duration",
        icon="mdi:clock-outline",
        value_fn=lambda d: _f(d, "print", "mc_remaining_time", cast=int)),
    BeambamSensorDesc(key="wifi", name="Wi-Fi signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class="signal_strength",
        value_fn=lambda d: _f(d, "print", "wifi_signal", default=None,
                              cast=lambda s: int(str(s).replace("dBm", "").strip()))),
    BeambamSensorDesc(key="filename", name="Print job", icon="mdi:file",
        value_fn=lambda d: dig(d, "print", "subtask_name", default="")),
    BeambamSensorDesc(key="stage", name="Stage", icon="mdi:state-machine",
        value_fn=lambda d: dig(d, "print", "gcode_state",
                               default=dig(d, "print", "mc_print_sub_stage",
                                           default="idle"))),
    BeambamSensorDesc(key="speed_profile", name="Speed profile",
        icon="mdi:speedometer",
        value_fn=lambda d: dig(d, "print", "spd_lvl", default="normal")),
    BeambamSensorDesc(key="chamber_fan", name="Chamber fan",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:fan",
        value_fn=lambda d: _f(d, "print", "big_fan1_speed", cast=int)),
    BeambamSensorDesc(key="aux_fan", name="Aux fan",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:fan",
        value_fn=lambda d: _f(d, "print", "big_fan2_speed", cast=int)),
    BeambamSensorDesc(key="cooling_fan", name="Cooling fan",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:fan",
        value_fn=lambda d: _f(d, "print", "cooling_fan_speed", cast=int)),
    BeambamSensorDesc(key="nozzle_diameter", name="Nozzle diameter",
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
        icon="mdi:printer-3d-nozzle",
        value_fn=lambda d: hwmap.nozzle_diameter_mm(
            dig(d, "print", "nozzle_diameter", default=""))),
    BeambamSensorDesc(key="nozzle_type", name="Nozzle type",
        icon="mdi:printer-3d-nozzle-alert",
        value_fn=lambda d: hwmap.nozzle_type_name(
            dig(d, "print", "nozzle_type", default=""))),
    BeambamSensorDesc(key="print_weight", name="Print weight",
        native_unit_of_measurement=UnitOfMass.GRAMS, device_class="weight",
        icon="mdi:weight-gram",
        value_fn=lambda d: _f(d, "print", "print_weight", cast=float, rnd=2)),
    BeambamSensorDesc(key="hms_count", name="HMS error count",
        icon="mdi:alert-circle",
        value_fn=lambda d: len(dig(d, "print", "hms", default=[]) or [])),
    BeambamSensorDesc(key="active_tray", name="Active AMS tray",
        icon="mdi:tray-full",
        value_fn=lambda d: (lambda n: (n + 1) if (n is not None and n != 255) else 0)(
            _f(d, "print", "ams", "tray_now", cast=int, default=255))),
]


def _ams_humidity(d, unit):
    return _f(d, "print", "ams", "ams", str(unit), "humidity", cast=float, rnd=0)


BINARY_SENSORS: list[BeambamBinaryDesc] = [
    BeambamBinaryDesc(key="online", name="Online", device_class="connectivity",
        value_fn=lambda d: dig(d, "print") is not None),
    BeambamBinaryDesc(key="door_open", name="Door open", device_class="door",
        value_fn=lambda d: _f(d, "print", "hw_switch_state", cast=int, default=0) == 1),
    BeambamBinaryDesc(key="hms_problem", name="HMS problem", device_class="problem",
        value_fn=lambda d: len(dig(d, "print", "hms", default=[]) or []) > 0),
    BeambamBinaryDesc(key="print_error", name="Print error", device_class="problem",
        value_fn=lambda d: _f(d, "print", "print_error", cast=int, default=0) != 0),
    BeambamBinaryDesc(key="timelapse_running", name="Timelapse running",
        device_class="running",
        value_fn=lambda d: dig(d, "print", "ipcam", "timelapse",
                               default="disable") == "enable"),
]

NUMBERS: list[BeambamNumberDesc] = [
    BeambamNumberDesc(key="bed_set", name="Bed setpoint", target="bed",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=0, native_max_value=110, native_step=1,
        mode="slider", icon="mdi:thermometer",
        value_fn=lambda d: _f(d, "print", "bed_target_temper", cast=int)),
    BeambamNumberDesc(key="nozzle_set", name="Nozzle setpoint", target="nozzle",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=0, native_max_value=320, native_step=1,
        mode="slider", icon="mdi:printer-3d-nozzle-heat",
        value_fn=lambda d: _f(d, "print", "nozzle_target_temper", cast=int)),
    BeambamNumberDesc(key="chamber_set", name="Chamber setpoint", target="chamber",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=0, native_max_value=60, native_step=1,
        mode="slider", icon="mdi:thermometer-lines",
        value_fn=lambda d: _f(d, "print", "chamber_target_temper", cast=int)),
]

# AMS slots: 1..16 across up to 4 units. Build per-slot color/material/filament.
AMS_SLOTS = 16
```

- [ ] **Step 3: Smoke-import**

Run: `python3.12 -c "from custom_components.beambam import descriptions, entity; print(len(descriptions.SENSORS),'sensors')"`
Expected: prints sensor count (≥ 20). (May require `homeassistant` importable; if not installed, this step is validated in A12 under the HA test env — note it.)

- [ ] **Step 4: Commit**

```bash
git add custom_components/beambam/entity.py custom_components/beambam/descriptions.py
git commit -m "feat(hacs): entity base + declarative description tables — Opus 4.8"
```

---

### Task A3: `sensor.py` (incl. AMS color/material/filament via colorsync)

**Files:**
- Create: `custom_components/beambam/sensor.py`
- Test: `tests/components/beambam/test_sensors.py`

- [ ] **Step 1: Implement**

```python
"""Sensor platform: scalar sensors + per-AMS-slot color/material/filament."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BeambamCoordinator
from .descriptions import SENSORS, AMS_SLOTS, BeambamSensorDesc
from .entity import BeambamEntity, dig


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    coord: BeambamCoordinator = hass.data[DOMAIN][entry.entry_id]
    ents: list[SensorEntity] = [BeambamSensor(coord, d) for d in SENSORS]
    for slot in range(1, AMS_SLOTS + 1):
        ents.append(BeambamAmsSensor(coord, slot, "color"))
        ents.append(BeambamAmsSensor(coord, slot, "material"))
        ents.append(BeambamAmsSensor(coord, slot, "filament"))
    add(ents)


class BeambamSensor(BeambamEntity, SensorEntity):
    def __init__(self, coordinator: BeambamCoordinator,
                 description: BeambamSensorDesc) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})


def _slot_tray(data: dict, slot: int) -> dict | None:
    """1-indexed global slot → tray dict (unit = (slot-1)//4, idx = (slot-1)%4)."""
    unit, idx = (slot - 1) // 4, (slot - 1) % 4
    return dig(data, "print", "ams", "ams", str(unit), "tray", str(idx))


class BeambamAmsSensor(BeambamEntity, SensorEntity):
    """Per-slot AMS attribute. `kind` ∈ {color, material, filament}.
    `filament` uses runtime.colorsync.mapper to name the closest Bambu profile.
    """
    def __init__(self, coordinator: BeambamCoordinator, slot: int, kind: str):
        super().__init__(coordinator, f"ams_slot{slot}_{kind}")
        self._slot = slot
        self._kind = kind
        self._attr_name = f"AMS slot {slot} {kind}"
        self._attr_icon = {
            "color": "mdi:palette", "material": "mdi:printer-3d-nozzle",
            "filament": "mdi:palette-swatch"}[kind]
        self._attr_entity_registry_enabled_default = slot <= 8

    @property
    def native_value(self) -> Any:
        tray = _slot_tray(self.coordinator.data or {}, self._slot)
        if not tray:
            return ""
        if self._kind == "color":
            raw = (tray.get("tray_color") or "")
            return ("#" + raw[:6]) if raw else ""
        if self._kind == "material":
            return tray.get("tray_type") or ""
        # filament: closest catalog match by color+material
        color = tray.get("tray_color") or ""
        if not color:
            return ""
        try:
            from runtime.colorsync.mapper import match as cs_match
        except Exception:  # noqa: BLE001
            return tray.get("tray_sub_brands") or ""
        m = cs_match(color, material=tray.get("tray_type"),
                     model=self.coordinator.model)
        return m.profile if m else (tray.get("tray_sub_brands") or "")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._kind != "filament":
            return {}
        tray = _slot_tray(self.coordinator.data or {}, self._slot) or {}
        return {"material": tray.get("tray_type", ""),
                "color": ("#" + (tray.get("tray_color") or "")[:6])
                if tray.get("tray_color") else "",
                "tray_info_idx": tray.get("tray_info_idx", "")}
```

- [ ] **Step 2: Test**

```python
# tests/components/beambam/test_sensors.py
import pytest
pytest.importorskip("pytest_homeassistant_custom_component")
from custom_components.beambam.sensor import _slot_tray, BeambamSensor
from custom_components.beambam.descriptions import SENSORS

SAMPLE = {"print": {"nozzle_temper": 211.7, "nozzle_type": "hardened_steel",
                    "nozzle_diameter": "0.4", "mc_percent": 42,
                    "ams": {"tray_now": "1", "ams": [
                        {"id": "0", "tray": [
                            {"tray_color": "F95D73FF", "tray_type": "PLA"},
                            {}, {}, {}]}]}}}


def test_scalar_value_fns():
    by = {d.key: d for d in SENSORS}
    assert by["nozzle_temp"].value_fn(SAMPLE) == 211.7
    assert by["nozzle_type"].value_fn(SAMPLE) == "Hardened Steel"
    assert by["nozzle_diameter"].value_fn(SAMPLE) == 0.4
    assert by["progress"].value_fn(SAMPLE) == 42
    assert by["active_tray"].value_fn(SAMPLE) == 2  # tray_now 1 → slot 2


def test_slot_tray_lookup():
    t = _slot_tray(SAMPLE, 1)
    assert t["tray_color"] == "F95D73FF"
    assert _slot_tray(SAMPLE, 2) == {}
```

- [ ] **Step 3: Run**

Run: `python3.12 -m pytest tests/components/beambam/test_sensors.py -v`
Expected: PASS (or SKIP if HA harness unavailable — record).

- [ ] **Step 4: Commit**

```bash
git add custom_components/beambam/sensor.py tests/components/beambam/test_sensors.py
git commit -m "feat(hacs): sensor platform incl. AMS color/material/filament — Opus 4.8"
```

---

### Task A4: `binary_sensor.py`

**Files:**
- Create: `custom_components/beambam/binary_sensor.py`

- [ ] **Step 1: Implement**

```python
"""Binary sensor platform."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BeambamCoordinator
from .descriptions import BINARY_SENSORS, BeambamBinaryDesc
from .entity import BeambamEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    coord: BeambamCoordinator = hass.data[DOMAIN][entry.entry_id]
    add(BeambamBinarySensor(coord, d) for d in BINARY_SENSORS)


class BeambamBinarySensor(BeambamEntity, BinarySensorEntity):
    def __init__(self, coordinator: BeambamCoordinator,
                 description: BeambamBinaryDesc) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        return bool(self.entity_description.value_fn(self.coordinator.data or {}))
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/beambam/binary_sensor.py
git commit -m "feat(hacs): binary_sensor platform — Opus 4.8"
```

---

### Task A5: `switch.py` + `light.py`

**Files:**
- Create: `custom_components/beambam/switch.py`, `custom_components/beambam/light.py`

- [ ] **Step 1: `light.py` (chamber light)**

```python
"""Light platform: chamber LED via ledctrl."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BeambamCoordinator
from .entity import BeambamEntity, dig


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    add([BeambamLight(hass.data[DOMAIN][entry.entry_id])])


class BeambamLight(BeambamEntity, LightEntity):
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_name = "Chamber light"

    def __init__(self, coordinator: BeambamCoordinator) -> None:
        super().__init__(coordinator, "chamber_light")
        self._attr_icon = "mdi:lightbulb"

    @property
    def is_on(self) -> bool:
        mode = dig(self.coordinator.data, "print", "lights_report", "0", "mode",
                   default="off")
        return mode == "on"

    def _cmd(self, mode: str) -> dict:
        from beambam.cli._helpers import _system_cmd
        return _system_cmd("ledctrl", led_node="chamber_light", led_mode=mode,
                           led_on_time=500, led_off_time=500,
                           loop_times=0, interval_time=0)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_publish(self._cmd("on"))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_publish(self._cmd("off"))
```

- [ ] **Step 2: `switch.py` (prompt sound)**

```python
"""Switch platform: prompt sound."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BeambamCoordinator
from .entity import BeambamEntity, dig


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    add([BeambamPromptSound(hass.data[DOMAIN][entry.entry_id])])


class BeambamPromptSound(BeambamEntity, SwitchEntity):
    _attr_name = "Prompt sound"

    def __init__(self, coordinator: BeambamCoordinator) -> None:
        super().__init__(coordinator, "prompt_sound")
        self._attr_icon = "mdi:volume-high"

    @property
    def is_on(self) -> bool:
        return dig(self.coordinator.data, "print", "sound_enable",
                   default="off") in ("on", True, "ON")

    def _cmd(self, enable: bool) -> dict:
        from beambam.cli._helpers import _print_cmd
        return _print_cmd("print_option", sound_enable=bool(enable))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_publish(self._cmd(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_publish(self._cmd(False))
```

- [ ] **Step 3: Verify command builders exist**

Run: `python3.12 -c "from beambam.cli._helpers import _print_cmd, _system_cmd; print('ok')"`
Expected: `ok`. (If `_system_cmd` signature differs, mirror the usage in `serve_http.py:915-920`.)

- [ ] **Step 4: Commit**

```bash
git add custom_components/beambam/switch.py custom_components/beambam/light.py
git commit -m "feat(hacs): switch (prompt sound) + light (chamber) platforms — Opus 4.8"
```

---

### Task A6: `button.py`

**Files:**
- Create: `custom_components/beambam/button.py`

- [ ] **Step 1: Implement**

```python
"""Button platform: print controls + maintenance + AMS load + start print."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (CONF_START_FILE, CONF_START_SLOT, DOMAIN)
from .coordinator import BeambamCoordinator
from .entity import BeambamEntity

# (key, name, icon)
_SIMPLE = [
    ("pause", "Pause print", "mdi:pause"),
    ("resume", "Resume print", "mdi:play"),
    ("stop", "Stop print", "mdi:stop"),
    ("home", "Home all axes", "mdi:home"),
    ("level", "Level bed", "mdi:format-line-style"),
    ("buzzer_silence", "Silence buzzer", "mdi:bell-off"),
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    coord: BeambamCoordinator = hass.data[DOMAIN][entry.entry_id]
    ents: list[ButtonEntity] = [BeambamButton(coord, k, n, i)
                                for k, n, i in _SIMPLE]
    for slot in range(1, 17):
        ents.append(BeambamAmsLoadButton(coord, slot))
    ents.append(BeambamStartPrintButton(coord))
    add(ents)


def _print_cmd(*a, **k):
    from beambam.cli._helpers import _print_cmd as pc
    return pc(*a, **k)


class BeambamButton(BeambamEntity, ButtonEntity):
    def __init__(self, coordinator, key, name, icon):
        super().__init__(coordinator, key)
        self._attr_name = name
        self._attr_icon = icon

    async def async_press(self) -> None:
        k = self._key
        if k in ("pause", "resume", "stop"):
            await self.coordinator.async_publish(_print_cmd(k, param=""))
        elif k == "home":
            await self.coordinator.async_publish(
                _print_cmd("gcode_line", param="G28\n"))
        elif k == "level":
            await self.coordinator.async_publish(
                _print_cmd("gcode_line", param="G29\n"))
        elif k == "buzzer_silence":
            await self.coordinator.async_publish(
                _print_cmd("gcode_line", param="M300 S0 P0\n"))


class BeambamAmsLoadButton(BeambamEntity, ButtonEntity):
    def __init__(self, coordinator, slot: int):
        super().__init__(coordinator, f"ams_slot{slot}_load")
        self._slot = slot
        self._attr_name = f"AMS slot {slot} load"
        self._attr_icon = "mdi:tray-arrow-up"
        self._attr_entity_registry_enabled_default = slot <= 8

    async def async_press(self) -> None:
        # ams_change_filament — target is 0-indexed global slot.
        await self.coordinator.async_publish(_print_cmd(
            "ams_change_filament", target=self._slot - 1,
            curr_temp=215, tar_temp=215))


class BeambamStartPrintButton(BeambamEntity, ButtonEntity):
    """Starts the entry's configured default file/slot (signed). For arbitrary
    files use the beambam.start_print service."""
    _attr_name = "Start print"

    def __init__(self, coordinator: BeambamCoordinator):
        super().__init__(coordinator, "start_print")
        self._attr_icon = "mdi:printer-3d"

    @property
    def available(self) -> bool:
        return super().available and bool(
            self.coordinator.entry.options.get(CONF_START_FILE)
            or self.coordinator.entry.data.get(CONF_START_FILE))

    async def async_press(self) -> None:
        opts = {**self.coordinator.entry.data, **self.coordinator.entry.options}
        gcode = opts.get(CONF_START_FILE)
        if not gcode:
            return
        await self.coordinator.async_start_print(
            gcode, slot=int(opts.get(CONF_START_SLOT, 0)))
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/beambam/button.py
git commit -m "feat(hacs): button platform (controls, AMS load, start print) — Opus 4.8"
```

---

### Task A7: `number.py`

**Files:**
- Create: `custom_components/beambam/number.py`

- [ ] **Step 1: Implement**

```python
"""Number platform: bed/nozzle/chamber setpoints."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import BeambamCoordinator
from .descriptions import NUMBERS, BeambamNumberDesc
from .entity import BeambamEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    coord: BeambamCoordinator = hass.data[DOMAIN][entry.entry_id]
    add(BeambamNumber(coord, d) for d in NUMBERS)


class BeambamNumber(BeambamEntity, NumberEntity):
    def __init__(self, coordinator: BeambamCoordinator,
                 description: BeambamNumberDesc) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data or {})

    async def async_set_native_value(self, value: float) -> None:
        from beambam.cli._helpers import _print_cmd
        target = self.entity_description.target
        v = int(value)
        if target == "bed":
            payload = _print_cmd("set_bed_temp", temp=v)
        elif target == "nozzle":
            payload = _print_cmd("set_nozzle_temp", extruder_index=0,
                                 target_temp=v)
        else:  # chamber
            payload = _print_cmd("gcode_line", param=f"M141 S{v}\n")
        await self.coordinator.async_publish(payload)
```

- [ ] **Step 2: Commit**

```bash
git add custom_components/beambam/number.py
git commit -m "feat(hacs): number platform (temp setpoints) — Opus 4.8"
```

---

### Task A8: `camera.py` + `image.py` (live stream — required)

**Files:**
- Create: `custom_components/beambam/camera.py`, `custom_components/beambam/image.py`

- [ ] **Step 1: `camera.py`**

```python
"""Camera platform — live stream (required).

RTSPS models (X1/X2D/H2D): expose stream_source() so HA's bundled stream/go2rtc
renders live HLS/WebRTC; still frames via ffmpeg.
Proprietary models (P1/A1): run beambam's lvl_local frame pump in an executor
thread into a latest-frame buffer; serve live MJPEG + stills.
An explicit camera-daemon URL option overrides both (still + mjpeg proxy).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (CONF_CAMERA_URL, CONF_RTSP_PORT, DEFAULT_RTSP_PORT, DOMAIN,
                    PROPRIETARY_CAM_MODELS)
from .coordinator import BeambamCoordinator
from .entity import BeambamEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    add([BeambamCamera(hass.data[DOMAIN][entry.entry_id])])


class BeambamCamera(BeambamEntity, Camera):
    _attr_name = "Chamber camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: BeambamCoordinator) -> None:
        BeambamEntity.__init__(self, coordinator, "chamber_camera")
        Camera.__init__(self)
        self._attr_icon = "mdi:camera"
        self._latest: bytes = b""
        self._pump_task = None
        opts = {**coordinator.entry.data, **coordinator.entry.options}
        self._override_url = opts.get(CONF_CAMERA_URL) or ""
        self._rtsp_port = int(opts.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT))
        self._proprietary = coordinator.model in PROPRIETARY_CAM_MODELS

    # ----- live stream -------------------------------------------------
    async def stream_source(self) -> str | None:
        if self._override_url:
            return None  # MJPEG-only override has no RTSP source
        if self._proprietary:
            return None  # lvl_local served via MJPEG, not RTSP
        c = self.coordinator
        return (f"rtsps://bblp:{c.access_code}@{c.host}:{self._rtsp_port}"
                f"/streaming/live/1")

    # ----- still -------------------------------------------------------
    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        if self._override_url:
            return await self._fetch_url(self._override_url.rstrip("/") + "/cam.jpg")
        if self._proprietary:
            return self._latest or None
        # RTSPS still via ffmpeg one-shot
        return await self._ffmpeg_still(await self.stream_source())

    async def _fetch_url(self, url: str) -> bytes | None:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(url, timeout=8) as r:
                if r.status == 200:
                    return await r.read()
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("camera still fetch failed: %s", e)
        return None

    async def _ffmpeg_still(self, source: str | None) -> bytes | None:
        if not source:
            return None
        from homeassistant.components.ffmpeg import get_ffmpeg_manager
        from haffmpeg.tools import ImageFrame, IMAGE_JPEG
        try:
            mgr = get_ffmpeg_manager(self.hass)
            frame = ImageFrame(mgr.binary)
            return await frame.get_image(
                source, output_format=IMAGE_JPEG,
                extra_cmd="-rtsp_transport tcp")
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("ffmpeg still failed: %s", e)
            return None

    # ----- proprietary lvl_local pump ----------------------------------
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._proprietary and not self._override_url:
            self._pump_task = self.hass.async_add_executor_job(self._pump)

    def _pump(self) -> None:
        """Blocking lvl_local frame pump (executor thread)."""
        try:
            import sys
            from beambam.presets import _x2d_search_roots
            for root in _x2d_search_roots():
                cand = root / "runtime" / "network_shim"
                if (cand / "lvl_local.py").exists():
                    sys.path.insert(0, str(cand)); break
            import lvl_local
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("lvl_local unavailable: %s", e)
            return
        c = self.coordinator

        def _store(jpeg, ts):
            self._latest = jpeg
        try:
            lvl_local.stream_frames(c.host, c.access_code, on_frame=_store)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("lvl_local pump ended: %s", e)
```

> NOTE: `haffmpeg.tools` is a HA dependency; the ffmpeg integration is built-in.
> If the import path differs in the target HA version, Task A12 surfaces it.

- [ ] **Step 2: `image.py` (retained snapshot fallback)**

```python
"""Image platform — last snapshot (works even when stream is idle)."""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import BeambamCoordinator
from .entity import BeambamEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add: AddEntitiesCallback) -> None:
    add([BeambamSnapshot(hass, hass.data[DOMAIN][entry.entry_id])])


class BeambamSnapshot(BeambamEntity, ImageEntity):
    _attr_name = "Chamber snapshot"

    def __init__(self, hass: HomeAssistant, coordinator: BeambamCoordinator):
        BeambamEntity.__init__(self, coordinator, "chamber_snapshot")
        ImageEntity.__init__(self, hass)
        self._attr_icon = "mdi:camera-image"
        self._cached: bytes = b""

    async def async_image(self) -> bytes | None:
        # Reuse the camera entity's still path via the platform's camera.
        from .camera import BeambamCamera  # local import avoids cycle
        cam = BeambamCamera(self.coordinator)
        cam.hass = self.hass
        img = await cam.async_camera_image()
        if img:
            self._cached = img
            self._attr_image_last_updated = dt_util.utcnow()
        return self._cached or None
```

- [ ] **Step 3: Commit**

```bash
git add custom_components/beambam/camera.py custom_components/beambam/image.py
git commit -m "feat(hacs): camera (live RTSPS/lvl_local stream) + image snapshot — Opus 4.8"
```

---

### Task A9: `config_flow.py` (user + signing: none/paste/file/cloud + options)

**Files:**
- Create: `custom_components/beambam/config_flow.py`
- Test: `tests/components/beambam/test_config_flow.py`

- [ ] **Step 1: Implement**

```python
"""Config + options flow with three signing methods."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback

from .const import (
    CONF_ACCESS_CODE, CONF_CAMERA_URL, CONF_CERT_ID, CONF_CLOUD_EMAIL,
    CONF_CLOUD_PASSWORD, CONF_DEVICE_CERT_PEM, CONF_DEVICE_CERT_PATH,
    CONF_CERT_ID_PATH, CONF_HOST, CONF_MODEL, CONF_RTSP_PORT, CONF_SCAN_INTERVAL,
    CONF_SERIAL, CONF_SIGN_KEY_PATH, CONF_SIGN_KEY_PEM, CONF_SIGNING_METHOD,
    CONF_START_FILE, CONF_START_SLOT, DEFAULT_MODEL, DEFAULT_RTSP_PORT,
    DEFAULT_SCAN_INTERVAL, DOMAIN, MODELS, SIGNING_CLOUD, SIGNING_FILE,
    SIGNING_NONE, SIGNING_PASTE,
)


async def _validate_printer(hass, host, code, serial) -> bool:
    """Connect once + request_state in the executor. Returns True on success."""
    from beambam.config import Creds
    from beambam.mqtt import X2DClient

    def _try() -> bool:
        cli = X2DClient(Creds(ip=host, code=code, serial=serial))
        cli.connect(timeout=8.0)
        try:
            cli.request_state(timeout=8.0)
            return True
        finally:
            cli.disconnect()
    try:
        return await hass.async_add_executor_job(_try)
    except Exception:  # noqa: BLE001
        return False


class BeambamConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_SERIAL])
            self._abort_if_unique_id_configured()
            ok = await _validate_printer(
                self.hass, user_input[CONF_HOST], user_input[CONF_ACCESS_CODE],
                user_input[CONF_SERIAL])
            if not ok:
                errors["base"] = "cannot_connect"
            else:
                self._data.update(user_input)
                return await self.async_step_signing()
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=self._data.get(CONF_HOST, "")): str,
            vol.Required(CONF_ACCESS_CODE): str,
            vol.Required(CONF_SERIAL, default=self._data.get(CONF_SERIAL, "")): str,
            vol.Required(CONF_MODEL, default=DEFAULT_MODEL): vol.In(MODELS),
        })
        return self.async_show_form(step_id="user", data_schema=schema,
                                    errors=errors)

    async def async_step_signing(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            method = user_input[CONF_SIGNING_METHOD]
            self._data[CONF_SIGNING_METHOD] = method
            if method == SIGNING_PASTE:
                return await self.async_step_sign_paste()
            if method == SIGNING_FILE:
                return await self.async_step_sign_file()
            if method == SIGNING_CLOUD:
                return await self.async_step_sign_cloud()
            return self._finish()
        schema = vol.Schema({
            vol.Required(CONF_SIGNING_METHOD, default=SIGNING_NONE):
                vol.In([SIGNING_NONE, SIGNING_PASTE, SIGNING_FILE, SIGNING_CLOUD]),
        })
        return self.async_show_form(step_id="signing", data_schema=schema)

    async def async_step_sign_paste(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_SIGN_KEY_PEM] = user_input[CONF_SIGN_KEY_PEM]
            self._data[CONF_CERT_ID] = user_input[CONF_CERT_ID]
            self._data[CONF_DEVICE_CERT_PEM] = user_input.get(CONF_DEVICE_CERT_PEM, "")
            return self._finish()
        schema = vol.Schema({
            vol.Required(CONF_SIGN_KEY_PEM): str,
            vol.Required(CONF_CERT_ID): str,
            vol.Optional(CONF_DEVICE_CERT_PEM, default=""): str,
        })
        return self.async_show_form(step_id="sign_paste", data_schema=schema)

    async def async_step_sign_file(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            kp = Path(user_input[CONF_SIGN_KEY_PATH])
            if not kp.is_file():
                errors["base"] = "file_not_found"
            else:
                self._data.update(user_input)
                self._data[CONF_CERT_ID] = user_input.get(CONF_CERT_ID, "")
                return self._finish()
        schema = vol.Schema({
            vol.Required(CONF_SIGN_KEY_PATH): str,
            vol.Required(CONF_CERT_ID_PATH): str,
            vol.Optional(CONF_DEVICE_CERT_PATH, default=""): str,
            vol.Optional(CONF_CERT_ID, default=""): str,
        })
        return self.async_show_form(step_id="sign_file", data_schema=schema,
                                    errors=errors)

    async def async_step_sign_cloud(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            ok = await self._cloud_login(user_input[CONF_CLOUD_EMAIL],
                                         user_input[CONF_CLOUD_PASSWORD])
            if not ok:
                errors["base"] = "cloud_auth_failed"
            else:
                self._data[CONF_CLOUD_EMAIL] = user_input[CONF_CLOUD_EMAIL]
                return self._finish()
        schema = vol.Schema({
            vol.Required(CONF_CLOUD_EMAIL): str,
            vol.Required(CONF_CLOUD_PASSWORD): str,
        })
        return self.async_show_form(step_id="sign_cloud", data_schema=schema,
                                    errors=errors)

    async def _cloud_login(self, email: str, password: str) -> bool:
        def _go() -> bool:
            from beambam.http_cloud import _http_cloud_login
            code, _ = _http_cloud_login(email=email, password=password)
            return code in (200, 0)
        try:
            return await self.hass.async_add_executor_job(_go)
        except Exception:  # noqa: BLE001
            return False

    @callback
    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=f"{self._data.get(CONF_MODEL, DEFAULT_MODEL)} "
                  f"({self._data[CONF_SERIAL]})",
            data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(entry):
        return BeambamOptionsFlow(entry)


class BeambamOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry) -> None:
        self.entry = entry

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        o = self.entry.options
        d = self.entry.data
        schema = vol.Schema({
            vol.Optional(CONF_CAMERA_URL,
                         default=o.get(CONF_CAMERA_URL, d.get(CONF_CAMERA_URL, ""))): str,
            vol.Optional(CONF_RTSP_PORT,
                         default=o.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): int,
            vol.Optional(CONF_SCAN_INTERVAL,
                         default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): int,
            vol.Optional(CONF_START_FILE,
                         default=o.get(CONF_START_FILE, "")): str,
            vol.Optional(CONF_START_SLOT,
                         default=o.get(CONF_START_SLOT, 0)): int,
        })
        return self.async_show_form(step_id="init", data_schema=schema)
```

- [ ] **Step 2: Test (flow happy paths)**

```python
# tests/components/beambam/test_config_flow.py
import pytest
pytest.importorskip("pytest_homeassistant_custom_component")
from unittest.mock import patch
from homeassistant import config_entries
from custom_components.beambam.const import (
    DOMAIN, CONF_HOST, CONF_ACCESS_CODE, CONF_SERIAL, CONF_MODEL,
    CONF_SIGNING_METHOD, SIGNING_NONE)


async def test_user_then_none(hass):
    with patch("custom_components.beambam.config_flow._validate_printer",
               return_value=True):
        res = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_HOST: "1.2.3.4", CONF_ACCESS_CODE: "12345678",
                             CONF_SERIAL: "03X2DTEST", CONF_MODEL: "X2D"})
        assert res["step_id"] == "signing"
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_SIGNING_METHOD: SIGNING_NONE})
        assert res["type"] == "create_entry"
        assert res["data"][CONF_SERIAL] == "03X2DTEST"


async def test_cannot_connect(hass):
    with patch("custom_components.beambam.config_flow._validate_printer",
               return_value=False):
        res = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER})
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {CONF_HOST: "1.2.3.4", CONF_ACCESS_CODE: "x",
                             CONF_SERIAL: "03X2DTEST", CONF_MODEL: "X2D"})
        assert res["errors"] == {"base": "cannot_connect"}
```

- [ ] **Step 3: Run**

Run: `python3.12 -m pytest tests/components/beambam/test_config_flow.py -v`
Expected: PASS (or SKIP — record).

- [ ] **Step 4: Commit**

```bash
git add custom_components/beambam/config_flow.py tests/components/beambam/test_config_flow.py
git commit -m "feat(hacs): config + options flow (none/paste/file/cloud signing) — Opus 4.8"
```

---

### Task A10: `__init__.py` (setup/unload + signing-material write + services)

**Files:**
- Modify: `custom_components/beambam/__init__.py` (full replace)
- Create: `custom_components/beambam/services.yaml`

- [ ] **Step 1: `__init__.py`**

```python
"""The beambam integration — setup, signing-material provisioning, services."""
from __future__ import annotations

import os
from pathlib import Path

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CERT_ID, CONF_DEVICE_CERT_PATH, CONF_DEVICE_CERT_PEM, CONF_CERT_ID_PATH,
    CONF_SERIAL, CONF_SIGN_KEY_PATH, CONF_SIGN_KEY_PEM, CONF_SIGNING_METHOD,
    DOMAIN, PLATFORMS, SIGNING_FILE, SIGNING_PASTE,
)
from .coordinator import BeambamCoordinator


def _provision_signing(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Write per-entry signing material to .storage/beambam/<entry_id>/ (0600)."""
    method = entry.data.get(CONF_SIGNING_METHOD, "none")
    dest = Path(hass.config.path(".storage", DOMAIN, entry.entry_id))
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)

    def _write(name: str, content: str) -> None:
        p = dest / name
        p.write_text(content)
        os.chmod(p, 0o600)

    if method == SIGNING_PASTE:
        if entry.data.get(CONF_SIGN_KEY_PEM):
            _write("printer_sign_key.pem", entry.data[CONF_SIGN_KEY_PEM])
        if entry.data.get(CONF_DEVICE_CERT_PEM):
            _write("printer_device_cert.pem", entry.data[CONF_DEVICE_CERT_PEM])
    elif method == SIGNING_FILE:
        for src_key, name in ((CONF_SIGN_KEY_PATH, "printer_sign_key.pem"),
                              (CONF_DEVICE_CERT_PATH, "printer_device_cert.pem")):
            src = entry.data.get(src_key)
            if src and Path(src).is_file():
                _write(name, Path(src).read_text())
        cid_path = entry.data.get(CONF_CERT_ID_PATH)
        if cid_path and Path(cid_path).is_file() and not entry.data.get(CONF_CERT_ID):
            # stash resolved cert_id into runtime (coordinator reads entry.data)
            hass.data.setdefault(DOMAIN + "_certid", {})[entry.entry_id] = \
                Path(cid_path).read_text().strip()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await hass.async_add_executor_job(_provision_signing, hass, entry)
    coordinator = BeambamCoordinator(hass, entry)
    # late cert_id from file method
    cid = hass.data.get(DOMAIN + "_certid", {}).get(entry.entry_id)
    if cid and not coordinator.cert_id:
        coordinator.cert_id = cid
    await coordinator.async_connect()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        coord: BeambamCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coord.async_shutdown()
    return ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "start_print"):
        return

    def _coord_for(call: ServiceCall) -> BeambamCoordinator:
        serial = call.data["serial"]
        for c in hass.data.get(DOMAIN, {}).values():
            if isinstance(c, BeambamCoordinator) and c.serial == serial:
                return c
        raise ValueError(f"no beambam printer with serial {serial!r}")

    async def start_print(call: ServiceCall) -> None:
        c = _coord_for(call)
        await c.async_start_print(
            call.data["gcode"], slot=call.data.get("slot", 0),
            use_ams=call.data.get("use_ams", True),
            local_path=call.data.get("local_path"),
            bed_type=call.data.get("bed_type"),
            bed_temp=call.data.get("bed_temp"),
            flow_cali=call.data.get("flow_cali", False),
            timelapse=call.data.get("timelapse", False))

    async def send_gcode(call: ServiceCall) -> None:
        from beambam.cli._helpers import _print_cmd
        c = _coord_for(call)
        line = call.data["line"]
        await c.async_publish(_print_cmd(
            "gcode_line", param=line if line.endswith("\n") else line + "\n"))

    hass.services.async_register(
        DOMAIN, "start_print", start_print,
        schema=vol.Schema({
            vol.Required("serial"): cv.string,
            vol.Required("gcode"): cv.string,
            vol.Optional("slot", default=0): vol.Coerce(int),
            vol.Optional("use_ams", default=True): cv.boolean,
            vol.Optional("local_path"): cv.string,
            vol.Optional("bed_type"): cv.string,
            vol.Optional("bed_temp"): vol.Coerce(int),
            vol.Optional("flow_cali", default=False): cv.boolean,
            vol.Optional("timelapse", default=False): cv.boolean,
        }))
    hass.services.async_register(
        DOMAIN, "send_gcode", send_gcode,
        schema=vol.Schema({vol.Required("serial"): cv.string,
                           vol.Required("line"): cv.string}))
```

- [ ] **Step 2: `services.yaml`**

```yaml
start_print:
  name: Start print
  description: Start a signed print of a file already on the printer (or upload via local_path).
  fields:
    serial:
      required: true
      example: "03X2DTEST"
      selector: { text: {} }
    gcode:
      required: true
      example: "model.gcode.3mf"
      selector: { text: {} }
    slot:
      default: 0
      selector: { number: { min: 0, max: 15, mode: box } }
    use_ams:
      default: true
      selector: { boolean: {} }
    local_path:
      selector: { text: {} }
    bed_type:
      selector: { text: {} }
    bed_temp:
      selector: { number: { min: 0, max: 120, mode: box } }
    flow_cali:
      default: false
      selector: { boolean: {} }
    timelapse:
      default: false
      selector: { boolean: {} }
send_gcode:
  name: Send G-code
  description: Send a raw G-code line to the printer.
  fields:
    serial:
      required: true
      selector: { text: {} }
    line:
      required: true
      example: "G28"
      selector: { text: {} }
```

- [ ] **Step 3: Commit**

```bash
git add custom_components/beambam/__init__.py custom_components/beambam/services.yaml
git commit -m "feat(hacs): setup/unload, signing-material provisioning, services — Opus 4.8"
```

---

### Task A11: `strings.json` + `translations/en.json` + `diagnostics.py`

**Files:**
- Create: `custom_components/beambam/strings.json`,
  `custom_components/beambam/translations/en.json` (identical content),
  `custom_components/beambam/diagnostics.py`

- [ ] **Step 1: `strings.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "Bambu Lab printer",
        "data": {
          "host": "Host / IP",
          "access_code": "LAN access code",
          "serial": "Serial number",
          "model": "Model"
        }
      },
      "signing": {
        "title": "Signed control",
        "description": "Choose how to provide the RSA signing material for signed print/control (X2D/H2D firmware). 'None' gives monitoring + basic control only.",
        "data": { "signing_method": "Signing method" }
      },
      "sign_paste": {
        "title": "Paste signing key",
        "data": {
          "sign_key_pem": "Sign key (PEM)",
          "cert_id": "Cert ID",
          "device_cert_pem": "Device cert (PEM, optional)"
        }
      },
      "sign_file": {
        "title": "Signing key files",
        "data": {
          "sign_key_path": "Sign key path",
          "cert_id_path": "Cert ID path",
          "device_cert_path": "Device cert path (optional)",
          "cert_id": "Cert ID (optional, overrides file)"
        }
      },
      "sign_cloud": {
        "title": "Bambu cloud login",
        "data": { "cloud_email": "Email", "cloud_password": "Password" }
      }
    },
    "error": {
      "cannot_connect": "Could not reach the printer (check IP, access code, serial).",
      "file_not_found": "Signing key file not found on the HA host.",
      "cloud_auth_failed": "Bambu cloud login failed."
    },
    "abort": { "already_configured": "This printer is already configured." }
  },
  "options": {
    "step": {
      "init": {
        "title": "Beambam options",
        "data": {
          "camera_url": "Camera daemon URL (optional override)",
          "rtsp_port": "RTSPS port",
          "scan_interval": "State poll interval (s)",
          "start_file": "Default print file (for Start print button)",
          "start_slot": "Default AMS slot"
        }
      }
    }
  }
}
```

- [ ] **Step 2: Copy to `translations/en.json`**

Run: `mkdir -p custom_components/beambam/translations && cp custom_components/beambam/strings.json custom_components/beambam/translations/en.json`

- [ ] **Step 3: `diagnostics.py`**

```python
"""Diagnostics — redacted config + last state."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (CONF_ACCESS_CODE, CONF_CLOUD_PASSWORD, CONF_DEVICE_CERT_PEM,
                    CONF_SIGN_KEY_PEM, DOMAIN)
from .coordinator import BeambamCoordinator

_REDACT = {CONF_ACCESS_CODE, CONF_SIGN_KEY_PEM, CONF_DEVICE_CERT_PEM,
           CONF_CLOUD_PASSWORD, "cert_id"}


async def async_get_config_entry_diagnostics(
        hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coord: BeambamCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": {"data": async_redact_data(dict(entry.data), _REDACT),
                  "options": dict(entry.options)},
        "state": coord.data or {},
    }
```

- [ ] **Step 4: Validate JSON + commit**

Run: `python3.12 -c "import json; json.load(open('custom_components/beambam/strings.json')); json.load(open('custom_components/beambam/translations/en.json')); print('ok')"`
Expected: `ok`.

```bash
git add custom_components/beambam/strings.json custom_components/beambam/translations/en.json custom_components/beambam/diagnostics.py
git commit -m "feat(hacs): strings, translations, diagnostics — Opus 4.8"
```

---

### Task A12: Integration test harness + full load

**Files:**
- Create: `tests/components/beambam/conftest.py`, `tests/components/beambam/test_init.py`

- [ ] **Step 1: `conftest.py`**

```python
import pytest
pytest.importorskip("pytest_homeassistant_custom_component")
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
```

- [ ] **Step 2: `test_init.py` (setup with a fake X2DClient)**

```python
import pytest
pytest.importorskip("pytest_homeassistant_custom_component")
from unittest.mock import patch, MagicMock
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.beambam.const import (
    DOMAIN, CONF_HOST, CONF_ACCESS_CODE, CONF_SERIAL, CONF_MODEL,
    CONF_SIGNING_METHOD, SIGNING_NONE)

SAMPLE = {"print": {"nozzle_temper": 200.0, "ams": {"ams": []}}}


async def test_setup_and_entities(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="03X2DTEST", data={
        CONF_HOST: "1.2.3.4", CONF_ACCESS_CODE: "x", CONF_SERIAL: "03X2DTEST",
        CONF_MODEL: "X2D", CONF_SIGNING_METHOD: SIGNING_NONE})
    entry.add_to_hass(hass)

    fake = MagicMock()
    fake.creds.serial = "03X2DTEST"
    with patch("custom_components.beambam.coordinator.X2DClient", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        # push a state so entities get values
        coord = hass.data[DOMAIN][entry.entry_id]
        coord.async_set_updated_data(SAMPLE)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    st = hass.states.get("sensor.bambu_lab_x2d_nozzle_temperature")
    assert st is not None
```

- [ ] **Step 3: Run the whole component suite**

Run: `python3.12 -m pytest tests/components/beambam/ -v`
Expected: PASS, or SKIPPED if `pytest-homeassistant-custom-component` won't
install on Termux. Record the outcome verbatim for Task P4.

- [ ] **Step 4: If the HA harness is unavailable**, do a structural lint instead:

Run:
```bash
python3.12 - <<'PY'
import ast, pathlib
for p in pathlib.Path("custom_components/beambam").rglob("*.py"):
    ast.parse(p.read_text())  # syntax check
print("all custom_components/beambam modules parse OK")
PY
```
Expected: `all custom_components/beambam modules parse OK`.

- [ ] **Step 5: Commit**

```bash
git add tests/components/beambam/
git commit -m "test(hacs): config_flow + setup + entity tests — Opus 4.8"
```

---

# PHASE P — packaging, docs, release, verification

### Task P1: README + `docs/HACS.md`

**Files:**
- Create: `docs/HACS.md`
- Modify: `README.md` (add a "Home Assistant (HACS)" section linking HACS.md)

- [ ] **Step 1: Write `docs/HACS.md`** covering: HACS custom-repo add
(`https://github.com/tribixbite/beambam`, category Integration), restart, Add
Integration → Beambam, the three signing methods (how to get the key via
`beambam key` on a machine with ADB+Handy, how to paste/file/cloud), camera notes
(RTSPS vs proprietary), services (`beambam.start_print`, `beambam.send_gcode`),
and a troubleshooting table. Use `beambam boo` where the daemon is mentioned.

- [ ] **Step 2: Add README section** (short, links to HACS.md + HA.md).

- [ ] **Step 3: Commit**

```bash
git add docs/HACS.md README.md
git commit -m "docs(hacs): install + signing + services guide; README HA section — Opus 4.8"
```

---

### Task P2: Update `docs/HA.md` + `docs/HA_SETUP.md` (boo rename + signed print)

**Files:**
- Modify: `docs/HA.md`, `docs/HA_SETUP.md`

- [ ] **Step 1: Replace `x2d_bridge.py daemon` / `daemon` invocations** with
`beambam boo` (and `python3.12 x2d_bridge.py ha-publish` → `beambam ha-publish`).

- [ ] **Step 2: Fix the §7 limitation row** in `HA_SETUP.md` — the
"nothing prints / known limitation" row now reads: signed start-print is
supported via the native integration's `beambam.start_print` service and the
bridge's `POST /control/start_print` / Start-print button when signing material
is configured. Keep the unsigned caveat for users without the key.

- [ ] **Step 3: Add a short "Native integration vs bridge" note** pointing to
`docs/HACS.md` as the no-extra-computer option.

- [ ] **Step 4: Commit**

```bash
git add docs/HA.md docs/HA_SETUP.md
git commit -m "docs(ha): boo rename + signed start-print; native-integration pointer — Opus 4.8"
```

---

### Task P3: Release 1.6.0 to PyPI (authorized)

**Files:** none (uses `.github/workflows/release.yml` OIDC trusted publishing)

- [ ] **Step 1: Pre-flight build**

Run: `python3.12 -m build` (or `pip install build` first). Confirm
`dist/beambam-1.6.0-*.whl` + sdist build cleanly and include
`custom_components`? (No — `custom_components` is NOT part of the wheel; it ships
via the git repo for HACS. The wheel ships `beambam` + `runtime`.) Verify the
wheel imports: `python3.12 -m pip install --force-reinstall dist/beambam-1.6.0-*.whl`
in a scratch venv, then `python -c "import beambam, beambam.hwmap"`.

- [ ] **Step 2: Ensure all phases merged to `main` and CI green**

Run: `gh run list --branch main --limit 3`

- [ ] **Step 3: Create the GitHub release (triggers PyPI publish)**

```bash
git tag v1.6.0
git push origin main --tags
gh release create v1.6.0 --title "v1.6.0 — Home Assistant native integration" \
  --notes-file - <<'EOF'
Native HACS integration (custom_components/beambam): HA Core runs beambam — no
extra host, no broker. Signed start-print, live camera, nozzle/color mapping,
three signing methods. Bridge gains POST /control/start_print + Start-print
button. See docs/HACS.md.
EOF
```

- [ ] **Step 4: Verify publish**

Run: `gh run watch` (release workflow), then
`python3.12 - <<'PY'
import urllib.request, json
d=json.load(urllib.request.urlopen("https://pypi.org/pypi/beambam/json"))
print("PyPI latest:", d["info"]["version"])
PY`
Expected: `PyPI latest: 1.6.0`.

> Gate: do NOT run Step 3 until the user has confirmed all prior phases are
> merged and tested. (User already authorized the publish itself.)

---

### Task P4: Live discovery verification + report

**Files:**
- Create: `docs/superpowers/plans/2026-06-23-ha-native-integration.md` is this
  plan; append a "Verification results" section OR write `docs/HA_VERIFY.md`.

- [ ] **Step 1: Bridge live discovery** — run on this device:

```bash
# in-process amqtt broker
python3.12 -m pytest runtime/ha/test_ha.py runtime/ha/test_bridge_e2e.py -v
# or a manual boo + ha-publish smoke against amqtt (document commands + output)
```
Capture the discovery topics + a START round-trip.

- [ ] **Step 2: Native integration** — run `tests/components/beambam/` and, if
the HA harness is unavailable, the AST parse lint (A12 Step 4). If a proot HA is
available (`docs/HA.md §4`), copy `custom_components/beambam` into its config dir,
restart, and confirm "Beambam" appears under Add Integration; screenshot/log it.

- [ ] **Step 3: Write the honest verification report** — what ran green, what was
skipped (and why, e.g. HA harness uninstallable on Termux), and the exact
remaining manual step for the user's live HA (add via HACS custom repo → Add
Integration → enter creds + signing).

- [ ] **Step 4: Commit**

```bash
git add docs/HA_VERIFY.md
git commit -m "docs(ha): verification results for native integration + bridge — Opus 4.8"
```

---

## Self-Review (completed)

- **Spec coverage:** native integration (A0-A12) ✓; signed start-print (C1, C3,
  A10 service, A6 button, B1) ✓; nozzle+color mapping (C2 hwmap, A3 colorsync
  consumption, B1 sensors) ✓; all-3 signing methods (A9, A10 provisioning) ✓;
  camera live stream (A8) ✓; HACS packaging (A0 hacs.json/manifest, P1) ✓;
  bridge gap-fix (B1-B2) ✓; PyPI 1.6.0 (C4, P3) ✓; `boo` rename (P2) ✓;
  discovery/zeroconf (A0 manifest) ✓; verification (P4) ✓.
- **Placeholder scan:** no TBD/TODO-as-spec; every code step has full code. Two
  explicit "verify against live/HA-version" NOTES (zeroconf service type;
  `haffmpeg` import path) are real-world unknowns flagged for A12, not skipped
  work.
- **Type/name consistency:** `BeambamCoordinator`, `BeambamEntity`, `dig`,
  `value_fn`, `_signing_paths`, `async_start_print`, `_provision_signing`,
  `CONF_*` constants used consistently across tasks; `start_print` kwargs
  (`sign_key_path`/`cert_id`/`device_cert_path`) match C1.

## Known compromises (call out during execution)
- `runtime` is a generic top-level package shipped in the beambam wheel; importing
  `runtime.colorsync.mapper` in HA's shared venv risks a name clash. Acceptable
  for v1 (pre-existing packaging choice); note in P1 docs. Mitigation if it bites:
  vendor the catalog into `custom_components/beambam/`.
- Cloud signing method uses `http_cloud`'s process-global `~/.x2d/cloud_session.json`
  → assumes a single Bambu account across entries. Document in HACS.md.
- HA-harness tests may be unskippable→skipped on Termux; AST lint is the floor.
