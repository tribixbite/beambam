"""beambam.cloud_slice — trigger a MakerWorld cloud-slice print via the REST API.

A cloud-slice print is ONE signed REST call (`POST /v1/user-service/my/task`);
the cloud then slices for the target printer, signs, and publishes the
`print.project_file` itself. This is the robust way for beambam to start a
print on a dual-nozzle X-series (X2D) — no local slice, no OSS upload, no
hand-built/signed MQTT command (which hit `err 84033544` for missing dual-nozzle
fields). Full protocol in `runtime/handy_extract/CLOUD_SLICE_API.md`.

The call needs two app-cert headers:
  * `x-bbl-app-certification-id: CN=GLOF<serial>.bambulab.com:<certSerialHex>`
    (the printer-control `cert_id` with its two halves swapped + `:`-joined).
  * `x-bbl-device-security-sign: base64(RSA-sign(PKCS#1v1.5-pad(ascii(now_ms))))`
    — a raw PKCS#1 v1.5 RSA signature of the *millisecond timestamp string*
    (NOT a hash of the body; the cloud extracts the ts for anti-replay and
    trusts the app cert). Signed with the recovered app key
    (`~/.x2d/printer_sign_key.pem`). Reproduces real Handy signatures byte-for-byte.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Optional

DEFAULT_KEY_PATH = Path.home() / ".x2d" / "printer_sign_key.pem"
DEFAULT_CERTID_PATH = Path.home() / ".x2d" / "printer_cert_id.txt"


def device_security_sign(private_key, ts_ms: int | str) -> str:
    """`x-bbl-device-security-sign` for the given ms timestamp.

    Raw PKCS#1 v1.5 (type 1) signature of the ASCII timestamp string — no
    SHA-256, no DigestInfo, the padded message is `00 01 FF…FF 00 || <ascii ts>`.
    """
    nums = private_key.private_numbers()
    n, d = nums.public_numbers.n, nums.d
    k = (n.bit_length() + 7) // 8
    data = str(ts_ms).encode("ascii")
    if len(data) > k - 11:
        raise ValueError("timestamp too long for the key modulus")
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(data)) + b"\x00" + data
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return base64.b64encode(sig).decode("ascii")


def app_certification_id(cert_id: str) -> str:
    """Map the printer-control `cert_id` (`<hex>CN=GLOF<serial>.bambulab.com`) to
    the HTTP header form (`CN=GLOF<serial>.bambulab.com:<hex>`)."""
    i = cert_id.find("CN=")
    if i <= 0:
        raise ValueError(f"unexpected cert_id form: {cert_id!r}")
    hex_part, cn_part = cert_id[:i], cert_id[i:]
    return f"{cn_part}:{hex_part}"


def signed_headers(private_key, cert_id: str, ts_ms: int) -> dict[str, str]:
    """The two app-cert headers every signed Bambu REST call carries."""
    return {
        "x-bbl-app-certification-id": app_certification_id(cert_id),
        "x-bbl-device-security-sign": device_security_sign(private_key, ts_ms),
    }


def build_cloud_slice_body(*, design_id: int, model_id: str, instance_id: int,
                           profile_id: int, title: str, cover: str, serial: str,
                           plate_index: int = 1, bed_type: str = "Supertack Plate",
                           ams_mapping: list[int], ams_detail_mapping: list[dict],
                           nozzle_infos: list[dict], filament_setting_ids: list[str],
                           use_ams: bool = True, nozzle_diameter: float = 0.4,
                           timelapse: bool = True, bed_leveling: bool = True,
                           flow_cali: bool = True) -> dict[str, Any]:
    """The `POST /v1/user-service/my/task` body (camelCase). Schema captured
    live from Handy — see CLOUD_SLICE_API.md. `mode:"cloud_slice"` makes the
    cloud slice for the device + publish the project_file itself."""
    return {
        "designId": design_id, "modelId": model_id, "instanceId": instance_id,
        "profileId": profile_id, "title": title, "cover": cover,
        "deviceId": serial, "nozzleDiameter": nozzle_diameter,
        "filamentSettingIds": filament_setting_ids,
        "plateIndex": plate_index, "plateName": "", "bedType": bed_type,
        "bedLeveling": bed_leveling, "flowCali": flow_cali, "timelapse": timelapse,
        "mode": "cloud_slice", "useAms": use_ams,
        "amsMapping": ams_mapping,
        "amsMapping2": [{"amsId": s // 4, "slotId": s % 4} for s in ams_mapping],
        "amsDetailMapping": ams_detail_mapping, "nozzleInfos": nozzle_infos,
        "hasFilamentSwitcher": 1, "isPublicProfile": True, "skipObjects": [],
        "repetitions": 1, "jobType": 1, "autoBedLeveling": 2, "extrudeCaliFlag": 2,
        "nozzleOffsetCali": 2, "extrudeCaliManualMode": 0,
        "primeVolumeMode": "Default", "enableArcFitting": 0,
        "matchFilamentMode": "Custom", "enableFilamentDynamicMap": 0,
        "deviceAccessories": {"enclosureKit": False},
    }


def cloud_slice_print(cli, body: dict, *, now_ms: int,
                      key_path: Path | str = DEFAULT_KEY_PATH,
                      cert_id: Optional[str] = None,
                      certid_path: Path | str = DEFAULT_CERTID_PATH) -> dict:
    """POST the cloud-slice task with the two signed app-cert headers + bearer.
    `now_ms` is injected for testability. Returns the parsed JSON response
    (the created task, with its id for polling `get_task`). `cli` is a
    `cloud_client.CloudClient`."""
    import cloud_client as _cc
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    key = load_pem_private_key(Path(key_path).read_bytes(), password=None)
    if cert_id is None:
        cert_id = Path(certid_path).read_text().strip()
    cli._ensure_fresh()
    url = _cc.REGIONS[cli.session.region]["iot"] + "/v1/user-service/my/task?ref_=beambam"
    headers = {**signed_headers(key, cert_id, now_ms),
               "Authorization": f"Bearer {cli.session.access_token}"}
    return _cc._request("POST", url, body=body, headers=headers)
