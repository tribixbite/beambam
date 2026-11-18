"""beambam.signing — can this machine sign X2D/H2D print control, and if not, why?

Jan-2025+ X2D/H2D firmware accepts `print.*` over LAN (print start, pause,
resume, stop, gcode) only when it is signed with a per-install key recovered
from Bambu Handy (`beambam key`), and the signed pre-image embeds the Bambu
account's `user_id` (`mqtt_sign.preimage`). When any input is missing or
stale the printer answers `err_code 84033545` / `84033543` ("mqtt message
verify failed") and nothing more. :func:`signing_status` lists every input
and its problem, so `doctor`, `print` and the control verbs can say what to
fix instead of only "refused".

Inputs (all in the config dir, see :mod:`beambam.paths`):

* ``printer_sign_key.pem`` + ``printer_cert_id.txt`` — from `beambam key`
* ``printer_app_cert.pem`` — the Handy app cert the key belongs to; its
  validity window is checked (an expired cert is the usual cause once a
  previously working setup starts failing)
* ``user_id`` — from ``cloud_session.json`` (`beambam cloud-login`) or
  ``$X2D_USER_ID`` (env or ``.env``)
* ``printer_device_cert.pem`` — `beambam device-cert`; needed for the X2D
  print start's ``url_enc``, not for other print control
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass, field

from beambam.paths import config_path

__all__ = ["SigningStatus", "signing_status", "SIGNATURE_REJECTED",
           "SIGNATURE_OK_DOWNSTREAM"]

# err_codes the firmware returns when it rejects a command's signature.
# 84033543 = missing/malformed signature (stage 1); 84033545 = cert not
# authorized for print.* OR an under-floor sequence_id (stage 2 — the code is
# overloaded, so a too-small seq looks like a cert-trust failure).
SIGNATURE_REJECTED = frozenset({84033543, 84033545})

# Stage 4: signature + cert + authorization ALL PASS; only a downstream
# file/task check failed (see runtime/handy_extract/DART_HEAP_KEY_EXTRACTION.md).
# A bare gcode_line (e.g. M400) on an idle printer has no task, so it lands here
# even though signing fully succeeded — NOT a signing failure.
SIGNATURE_OK_DOWNSTREAM = 84033544


@dataclass
class SigningStatus:
    key: bool = False
    cert_id: bool = False
    device_cert: bool = False
    user_id_source: str = ""                 # "cloud_session.json" | "X2D_USER_ID" | ""
    app_cert_expires: _dt.datetime | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """All inputs for LAN print control are present and current."""
        return not self.problems

    def summary(self) -> str:
        return "ready" if self.ready else "; ".join(self.problems)


def _user_id_source() -> str:
    try:
        j = json.loads(config_path("cloud_session.json").read_text(encoding="utf-8"))
        if j.get("user_id"):
            return "cloud_session.json"
    except (OSError, ValueError):
        pass
    return "X2D_USER_ID" if os.environ.get("X2D_USER_ID") else ""


def signing_status(now: _dt.datetime | None = None,
                   serial: str | None = None) -> SigningStatus:
    """Inspect the local signing inputs (no network). The key, cert id and app
    cert belong to the Handy install and are global; the device cert is per
    printer, so with a `serial` it is looked up under `<config dir>/<serial>/`
    first, falling back to the global file (what the print path uses)."""
    from beambam.paths import signing_path
    now = now or _dt.datetime.now(_dt.timezone.utc)
    st = SigningStatus(
        key=config_path("printer_sign_key.pem").is_file(),
        cert_id=config_path("printer_cert_id.txt").is_file(),
        device_cert=signing_path("printer_device_cert.pem", serial).is_file(),
        user_id_source=_user_id_source(),
    )
    if not st.key or not st.cert_id:
        st.problems.append("no recovered signing key / cert id — run `beambam key` "
                           "(Bambu Handy on an adb-connected phone)")
    app_cert = config_path("printer_app_cert.pem")
    if app_cert.is_file():
        try:
            from cryptography import x509
            cert = x509.load_pem_x509_certificate(app_cert.read_bytes())
            st.app_cert_expires = cert.not_valid_after_utc
            if cert.not_valid_after_utc < now:
                st.problems.append(
                    f"the Handy app cert expired {cert.not_valid_after_utc:%Y-%m-%d} — "
                    f"re-capture it and re-run `beambam key`")
            elif cert.not_valid_before_utc > now:
                st.problems.append("the Handy app cert is not valid yet (check the clock)")
        except (ValueError, OSError) as e:
            st.problems.append(f"the Handy app cert is unreadable ({e})")
    if not st.user_id_source:
        st.problems.append("no Bambu user_id for the signed pre-image — `beambam cloud-login`, "
                           "or set X2D_USER_ID (env or .env)")
    return st
