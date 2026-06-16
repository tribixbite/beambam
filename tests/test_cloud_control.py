"""tests/test_cloud_control.py — signed cloud-command construction (no network)."""
from __future__ import annotations
import json
import sys
import types
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric import rsa
from beambam import mqtt_sign
from beambam.cloud_control import CloudPrinter


def _fake_cc(uid="2000000001"):
    cc = types.SimpleNamespace()
    cc.session = types.SimpleNamespace(user_id=uid)
    cc._ensure_fresh = lambda: None
    cc.mqtt_broker = lambda: "us.mqtt.bambulab.com"
    cc.mqtt_credentials = lambda: ("u_" + uid, "TOKEN")
    return cc


def _printer():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cp = CloudPrinter(_fake_cc(), "00M09A000000000",
                      signer=mqtt_sign.key_signer(key),
                      cert_id="ABC123CN=GLOF1.bambulab.com",
                      region_broker="us.mqtt.bambulab.com")
    return cp, key


def test_build_produces_verifiable_signed_command():
    cp, key = _printer()
    msg = cp.build("print", {"command": "pause"})
    obj = json.loads(msg)
    assert obj["print"]["command"] == "pause"
    assert obj["user_id"] == "2000000001"
    assert "sequence_id" in obj["print"] and "timestamp" in obj["print"]
    assert obj["header"]["sign_alg"] == "RSA_SHA256"
    assert obj["header"]["cert_id"] == "ABC123CN=GLOF1.bambulab.com"
    assert mqtt_sign.verify_message(msg, key.public_key()) is True


def test_topics_and_conveniences_target_the_serial():
    cp, _ = _printer()
    assert cp.req == "device/00M09A000000000/request"
    assert cp.rep == "device/00M09A000000000/report"
    msg = cp.build("print", {"command": "skip_objects", "obj_list": [1, 5]})
    assert json.loads(msg)["print"]["obj_list"] == [1, 5]


# ----- cmd_pause routes to the cloud-signed path when a key is present --------

def test_cmd_pause_routes_to_cloud_signed(monkeypatch, tmp_path):
    import argparse
    import types
    from beambam.cli import control
    # a real key file so _signing_key_path().is_file() passes
    keyp = tmp_path / "k.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    keyp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.delenv("X2D_FORCE_LAN", raising=False)
    monkeypatch.setattr(control, "_signing_key_path", lambda: keyp)
    # fake cloud_client module + CloudPrinter
    sess = types.SimpleNamespace(empty=False, user_id="u")
    fake_cc = types.SimpleNamespace(
        CloudClient=types.SimpleNamespace(load_or_anonymous=lambda: types.SimpleNamespace(session=sess)))
    monkeypatch.setitem(__import__("sys").modules, "cloud_client", fake_cc)
    monkeypatch.setattr(control._config.Creds, "resolve",
                        classmethod(lambda cls, a: types.SimpleNamespace(serial="S1", ip=None)))
    captured = {}
    class _CP:
        @classmethod
        def from_config(cls, *a, **k): return cls()
        def command(self, fam, cmd, **k): captured["call"] = (fam, cmd); return {"result": "SUCCESS"}
    import beambam.cloud_control as ccmod
    monkeypatch.setattr(ccmod, "CloudPrinter", _CP)
    rc = control.cmd_pause(argparse.Namespace())
    assert rc == 0
    assert captured["call"] == ("print", {"command": "pause", "param": ""})


def test_cmd_skip_routes_to_cloud_signed(monkeypatch, tmp_path):
    """`beambam skip 3 7` → signed print.skip_objects with obj_list=[3,7]."""
    import argparse
    import types
    from beambam.cli import control
    keyp = tmp_path / "k.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    keyp.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.delenv("X2D_FORCE_LAN", raising=False)
    monkeypatch.setattr(control, "_signing_key_path", lambda: keyp)
    sess = types.SimpleNamespace(empty=False, user_id="u")
    fake_cc = types.SimpleNamespace(
        CloudClient=types.SimpleNamespace(load_or_anonymous=lambda: types.SimpleNamespace(session=sess)))
    monkeypatch.setitem(__import__("sys").modules, "cloud_client", fake_cc)
    monkeypatch.setattr(control._config.Creds, "resolve",
                        classmethod(lambda cls, a: types.SimpleNamespace(serial="S1", ip=None)))
    captured = {}
    class _CP:
        @classmethod
        def from_config(cls, *a, **k): return cls()
        def command(self, fam, cmd, **k): captured["call"] = (fam, cmd); return {"result": "SUCCESS"}
    import beambam.cloud_control as ccmod
    monkeypatch.setattr(ccmod, "CloudPrinter", _CP)
    rc = control.cmd_skip(argparse.Namespace(obj_ids=[3, 7]))
    assert rc == 0
    assert captured["call"] == ("print", {"command": "skip_objects",
                                          "obj_list": [3, 7]})
