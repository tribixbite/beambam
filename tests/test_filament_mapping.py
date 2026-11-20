"""tests/test_filament_mapping.py — filament → tray mapping, Filament Track
Switch topology, print confirmation and signed-command failure reporting.

Fixtures are synthetic but shaped like the live X2D (2 nozzles, 4× AMS 2 Pro
routed through the switch, `aux` bit 29 set) — see beambam/filament_mapping.py
for the Bambu Studio sources each rule comes from.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import zipfile
from pathlib import Path

import pytest

from beambam import filament_mapping as fm
from beambam import mqtt_sign

# --------------------------------------------------------------------------
# builders
# --------------------------------------------------------------------------

def make_3mf(tmp_path: Path, filaments: list[tuple[str, str, str, str]], *,
             used: dict[int, list[int]] | None = None, name: str = "job.gcode.3mf",
             switcher: bool = False, dynamic: bool = False) -> Path:
    """filaments: [(type, colour, profile_id, nozzle '1'|'2')]; used: {plate: [1-based ids]}."""
    used = used or {1: [1]}
    ps = {
        "filament_type": [f[0] for f in filaments],
        "filament_colour": [f[1] for f in filaments],
        "filament_ids": [f[2] for f in filaments],
        "filament_map": [f[3] for f in filaments],
        "nozzle_diameter": ["0.4", "0.4"],
        "has_filament_switcher": "1" if switcher else "0",
        "enable_filament_dynamic_map": "1" if dynamic else "0",
        "curr_bed_type": "Textured PEI Plate",
        "textured_plate_temp_initial_layer": ["65"] * len(filaments),
    }
    plates = []
    for plate, ids in used.items():
        fil = "".join(f'<filament id="{i}" type="{filaments[i - 1][0]}" '
                      f'color="{filaments[i - 1][1]}" used_g="1.5"/>' for i in ids)
        plates.append(f'<plate><metadata key="index" value="{plate}"/>{fil}</plate>')
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(ps))
        z.writestr("Metadata/slice_info.config", "<config>" + "".join(plates) + "</config>")
        for plate in used:
            z.writestr(f"Metadata/plate_{plate}.gcode", f"; plate {plate}\nG28\n")
    return path


def tray(i: int, ttype: str, colour: str, idx: str = "GFL99") -> dict:
    return {"id": str(i), "tray_type": ttype, "tray_color": colour + "FF",
            "tray_info_idx": idx, "tray_sub_brands": "", "remain": 50}


def make_state(units: list[tuple[str, list[dict]]], *, switch: bool = True,
               nozzles: int = 2) -> dict:
    """units: [(info_hex, trays)] with ids 0..n-1."""
    return {"print": {
        "aux": "22801004" if switch else "02801004",
        "device": {"extruder": {"info": [{"id": i} for i in range(nozzles)]}},
        "ams": {"ams": [{"id": str(n), "info": info, "tray": trays}
                        for n, (info, trays) in enumerate(units)]},
        "vir_slot": [{"id": "254", "tray_type": "PLA", "tray_color": "898989FF"},
                     {"id": "255", "tray_type": "PLA", "tray_color": "898989FF"}],
    }}


SWITCH_B, SWITCH_A = "10001E03", "11001E03"   # live X2D values
RIGHT_ONLY, LEFT_ONLY = "10000003", "10000103"  # bound to extruder 0 / 1, no switch


@pytest.fixture
def x2d_state() -> dict:
    return make_state([
        (SWITCH_B, [tray(0, "PETG", "#F72323", "GFG99"), tray(1, "PLA", "#7C4B00"),
                    tray(2, "PLA", "#D3C5A3"), tray(3, "PLA", "#F98C36")]),
        (SWITCH_A, [tray(0, "PETG", "#F4EE2A", "GFG99"), tray(1, "PETG", "#FCECD6", "GFG99"),
                    tray(2, "PLA", "#161616"), tray(3, "PLA", "#FFFFFF", "GFA19")]),
    ])


# --------------------------------------------------------------------------
# topology
# --------------------------------------------------------------------------

def test_topology_decodes_switch_routing(x2d_state):
    topo = fm.read_topology(x2d_state)
    assert topo.nozzle_count == 2 and topo.switch_installed and topo.switch_ready
    a1, b1 = topo.tray(0), topo.tray(4)
    assert a1.extruders == {fm.MAIN_EXTRUDER, fm.DEPUTY_EXTRUDER} and a1.switch_input == "B"
    assert b1.switch_input == "A"
    assert a1.label() == "A1 (slot 0)" and topo.tray(7).label() == "B4 (slot 7)"
    ext = topo.tray(254)
    assert ext.external and ext.extruders == {fm.DEPUTY_EXTRUDER}


def test_topology_without_switch_binds_each_ams_to_one_nozzle():
    st = make_state([(RIGHT_ONLY, [tray(0, "PLA", "#FFFFFF")]),
                     (LEFT_ONLY, [tray(0, "PLA", "#FFFFFF")])], switch=False)
    topo = fm.read_topology(st)
    assert not topo.switch_installed
    assert topo.tray(0).extruders == {fm.MAIN_EXTRUDER}
    assert topo.tray(4).extruders == {fm.DEPUTY_EXTRUDER}


def test_switch_routed_ams_without_switch_feeds_nothing():
    st = make_state([(SWITCH_B, [tray(0, "PLA", "#FFFFFF")])], switch=False)
    assert fm.read_topology(st).tray(0).extruders == frozenset()


def test_ams_ht_tray_id_is_unit_id():
    st = make_state([])
    st["print"]["ams"]["ams"] = [{"id": "128", "info": "10000004",
                                  "tray": [tray(0, "PLA", "#FFFFFF")]}]
    t = fm.read_topology(st).trays[0]
    assert t.tray_id == 128 and t.unit_type == "AMS HT"


# --------------------------------------------------------------------------
# project
# --------------------------------------------------------------------------

def test_read_project_reports_only_used_filaments_on_the_plate(tmp_path):
    p = make_3mf(tmp_path, [("PLA", "#FF0000", "GFA00", "1")] * 4
                 + [("PLA", "#FFFFFF", "GFA00", "2")], used={1: [5], 2: [1, 2]})
    one = fm.read_project(p, 1)
    assert [f.number for f in one.used] == [5] and one.total_filaments == 5
    assert one.used[0].nozzle == fm.MAIN_EXTRUDER          # filament_map "2" = right
    assert [f.number for f in fm.read_project(p, 2).used] == [1, 2]
    with pytest.raises(fm.MappingError, match="no sliced plate 3"):
        fm.read_project(p, 3)


# --------------------------------------------------------------------------
# auto mapping
# --------------------------------------------------------------------------

def test_auto_picks_closest_colour_of_same_type(tmp_path, x2d_state):
    p = make_3mf(tmp_path, [("PLA", "#F0F0F0", "GFL99", "1")])
    plan = fm.plan_print(fm.read_project(p), x2d_state)
    assert plan.assignments[0].tray.tray_id == 7          # white PLA, not the PETG
    assert not plan.errors


def test_single_filament_mapping_lands_on_its_own_index(tmp_path, x2d_state):
    """The bug this module fixes: a plate using only filament 5 must send
    [-1, -1, -1, -1, tray, …], not [tray, -1, …]."""
    p = make_3mf(tmp_path, [("PLA", "#000000", "", "1")] * 4
                 + [("PLA", "#161616", "GFL99", "1")], used={1: [5]})
    plan = fm.plan_print(fm.read_project(p), x2d_state)
    v0, v1 = plan.mapping_arrays()
    assert v0 == [-1, -1, -1, -1, 6, -1, -1, -1, -1, -1]
    assert v1[4] == {"ams_id": 1, "slot_id": 2} and v1[0] == {"ams_id": 255, "slot_id": 255}


def test_multi_filament_uses_distinct_trays(tmp_path, x2d_state):
    p = make_3mf(tmp_path, [("PLA", "#FFFFFF", "", "1"), ("PLA", "#FFFFFF", "", "2")],
                 used={1: [1, 2]})
    plan = fm.plan_print(fm.read_project(p), x2d_state)
    trays = [a.tray.tray_id for a in plan.assignments]
    assert trays[0] == 7 and trays[1] != 7 and len(set(trays)) == 2


def test_never_maps_across_material_class(tmp_path):
    st = make_state([(SWITCH_B, [tray(0, "PETG", "#FFFFFF", "GFG99")])])
    p = make_3mf(tmp_path, [("PLA", "#FFFFFF", "", "1")])
    plan = fm.plan_print(fm.read_project(p), st)
    assert plan.assignments[0].tray is None and plan.errors


def test_aesthetic_prints_rank_colour_before_profile(tmp_path, x2d_state):
    cols = [("PLA", "#7C4B00", "GFA00", "1"), ("PLA", "#F98C36", "GFA00", "1"),
            ("PLA", "#161616", "GFA00", "1")]
    p = make_3mf(tmp_path, cols, used={1: [1, 2, 3]})
    plan = fm.plan_print(fm.read_project(p), x2d_state)
    assert [a.tray.tray_id for a in plan.assignments] == [1, 3, 6]


# --------------------------------------------------------------------------
# reachability (no switch) + switch flags
# --------------------------------------------------------------------------

def test_left_nozzle_filament_cannot_use_right_only_ams(tmp_path):
    st = make_state([(RIGHT_ONLY, [tray(0, "PLA", "#FFFFFF")])], switch=False)
    p = make_3mf(tmp_path, [("PLA", "#FFFFFF", "", "1")])       # left nozzle
    plan = fm.plan_print(fm.read_project(p), st, slot=0)
    assert any("only feeds the right nozzle" in e for e in plan.errors)


def test_switch_mismatch_is_a_note_and_dynamic_map_is_refused(tmp_path, x2d_state):
    p = make_3mf(tmp_path, [("PLA", "#FFFFFF", "", "1")], switcher=False)
    assert any("Filament Track Switch" in n for n in
               fm.plan_print(fm.read_project(p), x2d_state).notes)
    d = make_3mf(tmp_path, [("PLA", "#FFFFFF", "", "1")], name="d.gcode.3mf",
                 switcher=True, dynamic=True)
    with pytest.raises(fm.MappingError, match="automatic nozzle mode"):
        fm.plan_print(fm.read_project(d), x2d_state)


# --------------------------------------------------------------------------
# explicit choices
# --------------------------------------------------------------------------

def test_parse_map_forms():
    assert fm.parse_map("1=6, 2=B3,3=ext-left,4=ext") == {0: 6, 1: 6, 2: 254, 3: 255}
    with pytest.raises(fm.MappingError):
        fm.parse_map("1=Z9")


def test_index_slots_rules(tmp_path):
    single = fm.read_project(make_3mf(tmp_path, [("PLA", "#FFF", "", "1")] * 3,
                                      used={1: [3]}))
    assert fm.index_slots(single, 7) == {2: 7}
    assert fm.index_slots(single, [-1, -1, 7]) == {2: 7}        # Bambu Studio form
    multi = fm.read_project(make_3mf(tmp_path, [("PLA", "#FFF", "", "1")] * 3,
                                     used={1: [1, 3]}, name="m.gcode.3mf"))
    assert fm.index_slots(multi, [4, 9]) == {0: 4, 2: 9}        # one per used filament
    with pytest.raises(fm.MappingError, match="uses 2 filaments"):
        fm.index_slots(multi, 4)


def test_explicit_empty_or_wrong_material_tray_is_an_error(tmp_path, x2d_state):
    x2d_state["print"]["ams"]["ams"][0]["tray"][1]["tray_type"] = ""
    p = make_3mf(tmp_path, [("PLA", "#FFFFFF", "", "1")])
    assert any("is empty" in e for e in
               fm.plan_print(fm.read_project(p), x2d_state, explicit={0: 1}).errors)
    assert any("different material" in e for e in
               fm.plan_print(fm.read_project(p), x2d_state, explicit={0: 0}).errors)


def test_no_ams_maps_to_the_nozzles_external_spool(tmp_path, x2d_state):
    p = make_3mf(tmp_path, [("PLA", "#898989", "", "2")])       # right nozzle
    plan = fm.plan_print(fm.read_project(p), x2d_state, use_ams=False)
    v0, v1 = plan.mapping_arrays()
    assert plan.assignments[0].tray.tray_id == 255 and not plan.use_ams
    assert v0[0] == -1 and v1[0] == {"ams_id": 255, "slot_id": 0}


# --------------------------------------------------------------------------
# project_file body
# --------------------------------------------------------------------------

def test_x2d_body_uses_plan_arrays_flags_and_plate(tmp_path):
    from beambam.print_job import build_x2d_project_file
    cert = tmp_path / "dev.pem"
    _write_self_signed(cert)
    body = build_x2d_project_file(
        "a.gcode.3mf", "00", bed_type="textured_plate", plate=2,
        ams_mapping=[-1, 6] + [-1] * 8,
        ams_mapping2=[{"ams_id": 255, "slot_id": 255}, {"ams_id": 1, "slot_id": 2}]
        + [{"ams_id": 255, "slot_id": 255}] * 8,
        bed_levelling=False, flow_cali=True, vibration_cali=True,
        device_cert_path=cert)
    assert body["ams_mapping"][:2] == [-1, 6]
    assert body["param"] == "Metadata/plate_2.gcode"
    assert (body["auto_bed_leveling"], body["extrude_cali_flag"]) == (0, 1)
    assert body["flow_cali"] is True and body["vibration_cali"] is True
    default = build_x2d_project_file("a.gcode.3mf", "00", bed_type="x", ams_slots=[11],
                                     device_cert_path=cert)
    # Defaults reproduce the Bambu Studio capture (auto levelling / auto flow cali).
    assert (default["auto_bed_leveling"], default["extrude_cali_flag"]) == (2, 2)
    assert default["ams_mapping"][0] == 11 and default["param"] == "Metadata/plate_1.gcode"


def _write_self_signed(path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=2))
            .not_valid_after(now + dt.timedelta(days=-1 if "expired" in path.name else 365))
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


# --------------------------------------------------------------------------
# confirmation
# --------------------------------------------------------------------------

def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**{"slot": None, "map": None, "yes": False, **kw})


def test_confirm_refuses_warnings_without_terminal(tmp_path, x2d_state, monkeypatch):
    from beambam.cli.lan import _plan_and_confirm
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    p = make_3mf(tmp_path, [("PLA", "#00FF00", "GFA00", "1")])   # no green loaded
    with pytest.raises(SystemExit, match="pass --yes"):
        _plan_and_confirm(_args(), x2d_state, p, use_ams=True, force=False)
    assert _plan_and_confirm(_args(yes=True), x2d_state, p, use_ams=True, force=False)


def test_confirm_prompt_no_cancels_and_errors_never_pass(tmp_path, x2d_state, monkeypatch):
    from beambam.cli.lan import _plan_and_confirm
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    p = make_3mf(tmp_path, [("PLA", "#FFFFFF", "GFA19", "1")])
    with pytest.raises(SystemExit, match="cancelled"):
        _plan_and_confirm(_args(), x2d_state, p, use_ams=True, force=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert _plan_and_confirm(_args(), x2d_state, p, use_ams=True, force=False)
    with pytest.raises(SystemExit, match="not bypassable"):
        _plan_and_confirm(_args(yes=True, map="1=0"), x2d_state, p, use_ams=True, force=True)


# --------------------------------------------------------------------------
# signed-command failures
# --------------------------------------------------------------------------

def test_explain_ack():
    assert mqtt_sign.explain_ack({"result": "success", "err_code": 0}) is None
    assert "certificate" in mqtt_sign.explain_ack(
        {"result": "failed", "reason": "mqtt message verify failed", "err_code": 84033545})
    assert "no reply" in mqtt_sign.explain_ack(None)


def test_cloud_signed_failure_exits_nonzero(monkeypatch, tmp_path):
    from beambam.cli import control
    monkeypatch.delenv("X2D_FORCE_LAN", raising=False)   # conftest forces LAN by default
    key = tmp_path / "k.pem"
    key.write_text("x")
    monkeypatch.setattr(control, "_signing_key_path", lambda: key)

    class _Session:
        empty = False

    class _CC:
        session = _Session()

        @classmethod
        def load_or_anonymous(cls):
            return cls()

    class _CP:
        @classmethod
        def from_config(cls, *a, **k):
            return cls()

        def command(self, family, cmd):
            return {"command": cmd["command"], "result": "failed", "err_code": 84033545}

    import sys
    import types
    monkeypatch.setitem(sys.modules, "cloud_client", types.SimpleNamespace(CloudClient=_CC))
    monkeypatch.setattr("beambam.cloud_control.CloudPrinter", _CP)
    monkeypatch.setattr(control._config.Creds, "resolve",
                        classmethod(lambda cls, a: types.SimpleNamespace(serial="SN")))
    rc = control._try_cloud_signed(argparse.Namespace(lan=False),
                                   {"print": {"command": "pause"}})
    assert rc == 1


def test_doctor_flags_expired_signing_cert(tmp_path):
    from beambam.doctor import _signing_cert_check
    good, bad = tmp_path / "good.pem", tmp_path / "expired.pem"
    _write_self_signed(good)
    _write_self_signed(bad)
    assert _signing_cert_check(good).severity == "pass"
    c = _signing_cert_check(bad)
    assert c.severity == "fail" and "84033545" in c.detail
