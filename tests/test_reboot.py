"""Tests for `beambam reboot`.

Bambu firmware doesn't expose a clean MQTT reboot verb, so the
implementation sends `M999` (Marlin "restart from emergency stop")
via the existing gcode_line signed-MQTT pipe. The command is
deliberately dry-run-by-default — the wording "reboot" is broader
than what M999 actually does, and we don't want a misclick to wipe
mid-print error state. The user must pass `--confirm` to actually
publish.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: F401

# `bridge` was `import x2d_bridge as bridge` before the v1.5 split;
# cmd_reboot + _reboot_payload + _publish_one now live in
# beambam.cli.control, which is the patch target the tests below
# reach via `patch.object(bridge, ...)`.
from beambam.cli import control as bridge

from beambam.cli.control import _reboot_payload, cmd_reboot


def test_reboot_payload_is_m999_via_gcode_line():
    """The wire payload should be `gcode_line` with `M999\\n` — the
    same shape `cmd_gcode` produces, so it flows through the existing
    signed-MQTT path with no special handling needed."""
    payload = _reboot_payload()
    assert payload["print"]["command"] == "gcode_line"
    assert payload["print"]["param"] == "M999\n"
    # sequence_id is monotonic — present and non-empty
    assert payload["print"]["sequence_id"]


def test_reboot_dry_run_is_default_and_does_not_publish():
    """Without --confirm: print explanation + intended payload to
    stderr, exit 0, and crucially NEVER call _publish_one (so the
    printer is untouched and Creds.resolve isn't invoked)."""
    args = argparse.Namespace(confirm=False,
                              ip=None, code=None, serial=None, printer=None)
    buf = io.StringIO()
    with patch.object(bridge, "_publish_one") as pub, \
         redirect_stderr(buf):
        rc = cmd_reboot(args)
    assert rc == 0
    pub.assert_not_called()
    err = buf.getvalue()
    assert "DRY-RUN" in err
    assert "--confirm" in err
    assert "M999" in err  # implied via payload echo
    # The "this is not a real reboot" warning must be present so
    # someone reading the dry-run output doesn't assume it works.
    assert "power-cycle" in err
    # The dry-run echo must be valid JSON the user can pipe.
    json_lines = [ln for ln in err.splitlines()
                  if "would publish:" in ln]
    assert len(json_lines) == 1
    blob = json_lines[0].split("would publish:", 1)[1].strip()
    parsed = json.loads(blob)
    assert parsed["print"]["param"] == "M999\n"


def test_reboot_confirm_calls_publish_one_with_m999():
    """With --confirm: _publish_one runs with the M999 payload. We
    stub _publish_one so the test doesn't actually need a printer."""
    args = argparse.Namespace(confirm=True,
                              ip=None, code=None, serial=None, printer=None)
    with patch.object(bridge, "_publish_one", return_value=0) as pub:
        rc = cmd_reboot(args)
    assert rc == 0
    assert pub.call_count == 1
    _, sent_payload = pub.call_args[0]
    assert sent_payload["print"]["param"] == "M999\n"
    assert sent_payload["print"]["command"] == "gcode_line"


def test_reboot_confirm_propagates_publish_failure():
    """If the signed-MQTT publish fails, the failure code propagates so
    callers (CI, shell scripts) can react."""
    args = argparse.Namespace(confirm=True,
                              ip=None, code=None, serial=None, printer=None)
    with patch.object(bridge, "_publish_one", return_value=3):
        rc = cmd_reboot(args)
    assert rc == 3
