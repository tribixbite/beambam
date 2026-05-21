"""Guard test: `x2d_bridge.py` must not grow more `cmd_*` handlers.

The bridge-split plan (`docs/BRIDGE_SPLIT_PLAN.md`) calls for `cmd_*`
handlers to drain from the monolith into `beambam/cli/*.py` over a
series of phases. Without an active guard, new features land in the
monolith because the existing import path is convenient, and the
"deferred to v1.X" excuse never expires.

This test pins the current `cmd_*` count and FAILS CI if it grows.
The intended workflow:

  * To **add a new command**, put it in `beambam/cli/<group>.py` and
    register the handler from there. The count in this file stays the
    same.
  * To **finish a Phase 5 sub-phase**, move a batch of handlers out
    of `x2d_bridge.py` and lower `_MAX_CMD_HANDLERS_IN_BRIDGE`
    accordingly. The lowering is the visible record of progress.
  * To **bypass the guard** (only legitimate when refactoring this
    file or the bridge itself), update both this number AND the
    plan doc in the same commit. A reviewer can see the change.

The guard's *only* job is to prevent silent regression. It does not
say *which* handlers belong where — that's the plan doc's job.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: F401  (registered for collection only)


REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "x2d_bridge.py"

# Pinned count as of 2026-05-21. To lower this number, run:
#
#   grep -cE "^def cmd_" x2d_bridge.py
#
# after a Phase 5 sub-phase lands and paste the new number here in the
# same commit that does the migration. To RAISE this number, you need a
# real reason — talk to the maintainer first or update
# docs/BRIDGE_SPLIT_PLAN.md to acknowledge the new floor.
# History:
#   74 — Phase 4 close (X2DClient / metrics → beambam.mqtt)
#   71 — Phase 5b incremental: cloud-logout, cloud-search-suggest,
#        cloud-app-config moved into beambam.cli.cloud (cloud-ttcode
#        moved with the scaffold)
#   65 — Phase 5b incremental: cloud-history, cloud-task,
#        cloud-messages, cloud-tickets, cloud-firmware, cloud-filaments
#        moved into beambam.cli.cloud
#   58 — Phase 5b: cloud-search, cloud-browse, cloud-design,
#        cloud-design-remixes, cloud-favorites, cloud-liked,
#        cloud-presets moved into beambam.cli.cloud
#   54 — Phase 5b: cloud-feed, cloud-like, cloud-comments,
#        cloud-comment-reply moved into beambam.cli.cloud
#   49 — Phase 5b complete-ish: cloud-pause, cloud-resume, cloud-stop,
#        cloud-gcode, cloud-chamber-light moved into beambam.cli.cloud
#        (via lazy thunks back into x2d_bridge for the cloud-MQTT
#        publish helper). The 4 large cloud handlers (login,
#        pull/print-design, get-access-code, print, publish, state,
#        printers, status) still live in the monolith — they pull in
#        too many bridge internals to move cleanly without dragging
#        the daemon machinery with them. Tracked separately.
#   41 — Phase 5a sub-batch: pause, resume, stop, gcode, home, level,
#        set-temp, chamber-light moved into beambam.cli.control (LAN
#        siblings of the cloud-control verbs migrated at 49). Lazy
#        thunk back into x2d_bridge._publish_one keeps the LAN
#        connect/sign/ack-wait state machine in the monolith for now.
#   36 — Phase 5a batch 2: reboot, jog, record, timelapse, resolution
#        moved into beambam.cli.control. `_reboot_payload` helper +
#        `_REBOOT_GCODE` constant stay in x2d_bridge so existing test
#        imports keep working — cmd_reboot lazy-thunks into them.
#   33 — Phase 5a batch 3: fod-check, ams-load, ams-unload moved into
#        beambam.cli.control. `_xcam_cmd` helper relocated into
#        beambam.cli._helpers (re-exported from x2d_bridge for back-
#        compat). Only cmd_files remains from the original Phase 5a
#        list — its FTPS/Creds wiring is too entangled to move cleanly.
#   31 — Phase 5c batch 1: cmd_status + cmd_printers moved into a new
#        beambam.cli.info module. Both are pure-read MQTT-only or
#        local-config readers, so they have zero coupling to the
#        publish/connect state machine.
#   29 — Phase 5c batch 2: cmd_health + cmd_watch moved into
#        beambam.cli.info. cmd_health is a one-shot TCP+MQTT+AMS
#        diagnostic; cmd_watch is a polling status loop. Both are
#        zero-coupling reads.
#   27 — Phase 5c batch 3: cmd_tail (with _TailDispatcher class +
#        _tail_print helper) + cmd_notify moved into beambam.cli.info.
#        Class + helper are re-exported from x2d_bridge so the
#        existing tail unit tests (`from x2d_bridge import
#        _TailDispatcher, _tail_print`) keep working unchanged.
#   26 — Phase 5c batch 4: cmd_fetch (250-LoC multi-host URL parser
#        for MakerWorld / Printables / Thingiverse / direct STL/3MF)
#        moved into beambam.cli.info. Lazy-thunks back into x2d_bridge
#        for PACKAGE_VERSION + X2D_ROOT_PATH. Phase 5c CLOSED — all
#        7 listed read-only handlers now live in beambam.cli.info.
#   21 — Phase 5b batch 6: cmd_cloud_printers + cmd_cloud_status +
#        cmd_cloud_spool_{add,update,delete} moved into
#        beambam.cli.cloud. `_spool_body_from_args` +
#        `_require_allow_write` helpers relocated too — re-exported
#        for back-compat. Remaining cloud handlers in monolith are
#        the heavy ones (login, state, pull/print-design,
#        get-access-code, print, publish) tied to bridge internals.
#   20 — Phase 5b batch 7: cmd_cloud_state moved into beambam.cli.cloud
#        along with the three cloud-MQTT helpers it depends on:
#        _cloud_mqtt_connect, _cloud_publish_payload,
#        _resolve_cloud_serial. The cloud-control verbs that used to
#        lazy-thunk back into x2d_bridge for these helpers now call
#        them directly inside cloud.py — same module, no indirection.
#   19 — Phase 5b batch 8: cmd_cloud_login (127 LoC — three auth
#        flows + bootstrap loop) moved into beambam.cli.cloud. The
#        bootstrap path lazy-imports cmd_cloud_get_access_code from
#        x2d_bridge until that handler moves in a follow-up batch.
#   18 — Phase 5b batch 9: cmd_cloud_get_access_code moved into
#        beambam.cli.cloud. Closes the lazy thunk that cmd_cloud_login
#        had against the bridge — the bootstrap loop now calls a
#        same-module helper directly.
_MAX_CMD_HANDLERS_IN_BRIDGE = 18


def _count_cmd_handlers() -> int:
    """Count top-level `def cmd_*` declarations in x2d_bridge.py.

    Match anchor on column 0 only — nested defs inside other functions
    don't count as new public CLI handlers."""
    src = BRIDGE.read_text(encoding="utf-8")
    return len(re.findall(r"^def cmd_", src, re.MULTILINE))


def test_cmd_handler_count_does_not_grow():
    """Pinned at 74. If you bumped it because of a feature add: STOP,
    put the handler in beambam/cli/ instead. See
    `docs/BRIDGE_SPLIT_PLAN.md` Phase 5 for the migration path."""
    actual = _count_cmd_handlers()
    assert actual <= _MAX_CMD_HANDLERS_IN_BRIDGE, (
        f"x2d_bridge.py now has {actual} cmd_* handlers — "
        f"the pinned ceiling is {_MAX_CMD_HANDLERS_IN_BRIDGE}. New CLI "
        "commands must land in `beambam/cli/<group>.py`, not the "
        "monolith. See `docs/BRIDGE_SPLIT_PLAN.md` for which group your "
        "handler belongs in. If you genuinely need to bypass the guard "
        "(refactoring the bridge itself), lower the floor in this file "
        "in the same commit that does the work."
    )


def test_cmd_handler_count_is_not_stale():
    """If you completed a Phase 5 sub-phase, the pinned ceiling should
    be lowered to match. Catches the opposite drift: a successful
    migration whose author forgot to update this number, hiding future
    progress behind a stale ceiling."""
    actual = _count_cmd_handlers()
    # Tolerate the count being one or two below pinned — handlers
    # sometimes get inlined or merged in routine cleanup. Beyond 5
    # under, the ceiling is genuinely stale and should be lowered.
    drift = _MAX_CMD_HANDLERS_IN_BRIDGE - actual
    assert drift <= 5, (
        f"x2d_bridge.py has {actual} cmd_* handlers but the pinned "
        f"ceiling is {_MAX_CMD_HANDLERS_IN_BRIDGE} (drift {drift}). "
        "Lower `_MAX_CMD_HANDLERS_IN_BRIDGE` in tests/test_bridge_split_progress.py "
        "to the new actual count — visible progress is the point of this test."
    )
