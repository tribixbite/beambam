"""Guard test: `x2d_bridge.py` must not grow more `cmd_*` handlers.

The bridge-split plan (`docs/BRIDGE_SPLIT_PLAN.md`) called for `cmd_*`
handlers to drain from the monolith into `beambam/cli/*.py` over a
series of phases. As of commit 404352f (Phase 5d batch 8, 2026-05-21)
**that goal is complete** — `grep -c '^def cmd_' x2d_bridge.py`
returns 0. Every CLI command now lives under `beambam/cli/{cloud,
control,daemon,info,lan}.py`, and `x2d_bridge.py` re-exports each
handler for back-compat.

This test still exists as a regression guard: it FAILS CI if a new
`cmd_*` handler is ever re-added to the monolith. The intended
workflow going forward:

  * To **add a new command**, put it in `beambam/cli/<group>.py` and
    register the handler from there. The count in this file stays
    at zero.
  * To **bypass the guard** (only legitimate when refactoring the
    monolith itself), update both this number AND the plan doc in
    the same commit. A reviewer can see the change.

The remaining bridge-split work (Phase 5e — _serve_http /
ServeServer / main()) doesn't add new `cmd_*` handlers, so this
guard stays at zero.

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
#   16 — Phase 5b batch 10: cmd_cloud_print (120 LoC project_file
#        upload+publish chain) + cmd_cloud_publish (43 LoC raw JSON
#        publish) moved into beambam.cli.cloud. Both depend only on
#        _cloud_mqtt_connect which is already there since batch 7.
#   14 — Phase 5b batch 11: cmd_cloud_pull_design + cmd_cloud_print_design
#        moved. Phase 5b CLOSED — every cmd_cloud_* handler now lives
#        in beambam.cli.cloud. Monolith only has the LAN-print/serve/
#        daemon handlers left (Phase 5d) plus main() argparse builder
#        (Phase 5e).
#   12 — Phase 5c batch 5: cmd_analyze (3MF inspection wrapper) +
#        cmd_fcm_harvest (subprocess wrapper for the Handy FCM
#        snapshot harvester) moved into beambam.cli.info. Both are
#        read-only utility handlers; lazy-import X2D_ROOT_PATH for
#        the harvester-script path.
#   10 — Phase 5b batch 12: cmd_printables_search + cmd_print_search +
#        _print_search_printables helper moved into beambam.cli.cloud.
#        These are catalog-search handlers — they belong with the
#        cloud-search/browse/design family already there.
#    8 — Phase 5d batch 1: scaffold beambam.cli.lan with cmd_upload
#        (FTPS upload) + cmd_files (FTPS SD-card listing). Also
#        retroactively fixed Creds imports (beambam.creds →
#        beambam.config; the wrong path had been latent across info.py
#        handlers since Phase 5c — caught here because cmd_files has
#        actual test coverage that triggers the lazy import.)
#    7 — Phase 5c batch 6: cmd_help (argparse-internal alias for
#        `<topic> --help`) moved into beambam.cli.info. Self-contained
#        — only argparse + sys deps; uses `args._root_parser` threaded
#        in by add_subparser at main()-time.
#    5 — Phase 5d batch 2: scaffold beambam.cli.daemon with cmd_webrtc
#        (delegates to runtime.webrtc.server) + cmd_ha_publish (one
#        HAPublisher per credentials section). Both are long-running
#        background services that park on SIGINT/SIGTERM. The biggest
#        daemons (cmd_camera, cmd_serve, cmd_daemon) still need their
#        supporting class hierarchies (ServeServer, etc.) hoisted out
#        of the monolith before they can move cleanly.
#    4 — Phase 5d batch 4: cmd_slice_print (110 LoC end-to-end
#        STL→slice→upload→print pipeline) moved into beambam.cli.lan.
#        Migration unblocked by batch 3's print_job.py extraction —
#        all deps (start_print, _validate_ams_slot,
#        _derive_print_params_from_3mf, Creds, upload_file, X2DClient)
#        now live in beambam.* packages, no lazy thunks needed.
#    3 — Phase 5d batch 5: cmd_print (117 LoC LAN-direct print with
#        --dry-run analyzer + bed/temp/filament safety derivation +
#        AMS-slot live-state validation) moved into beambam.cli.lan.
#        Sibling of cmd_slice_print — same dependency surface.
#    2 — Phase 5d batch 6: cmd_camera (~455 LoC RTSPS→MJPEG/HLS proxy
#        daemon — on-demand pump + supervisor + HTTP server) moved
#        into beambam.cli.daemon. Lazy-imports `_check_bearer` and
#        `_x2d_search_roots` from x2d_bridge. Remaining handlers
#        (cmd_serve + cmd_daemon) both use the ~1080-LoC ServeServer
#        class still in monolith — that's a Phase 5d batch 7 target.
#    1 — Phase 5d batch 7: cmd_serve (3 LoC) lazy-thunks
#        x2d_bridge.ServeServer until the class itself can be
#        extracted in a later batch. Only cmd_daemon left in monolith
#        — it depends on the ~580-LoC _serve_http function.
#    0 — Phase 5d batch 8 (closes Phase 5d): cmd_daemon (200 LoC
#        multi-printer daemon) moved into beambam.cli.daemon with
#        lazy thunks for LOG_QUEUE, _serve_http, _WEB_DIR_DEFAULT.
#        Bridge cmd_* count: ZERO. Every CLI handler now lives under
#        beambam.cli.{cloud,control,daemon,info,lan}; x2d_bridge.py
#        is just argparse construction + the ServeServer / _serve_http
#        bodies + module-level constants + re-exports for back-compat.
_MAX_CMD_HANDLERS_IN_BRIDGE = 0


# x2d_bridge.py LoC ratchet — new in Phase 5e. With cmd_* at floor,
# the only useful drainage signal left is "did x2d_bridge.py shrink?".
# Lower this ceiling in the same commit that does the work; never
# raise it without an explicit reason in the commit message.
#
# History (file is gitignored from old artefacts — these LoC counts
# come from `wc -l x2d_bridge.py` at commit time):
#   3,470 — Phase 5d closed (cmd_* hit 0)
#   3,462 — Phase 5e.1: PACKAGE_VERSION → beambam/_version.py
#   2,501 — Phase 5e.2: ServeServer + _PrinterSession + _ConnHandler
#           + 14 _op_* + _OPS table → beambam/serve_socket.py
#
# Target after 5e.3 (extract _serve_http body): ~1,540
# Target after 5e.4 (extract _publish_one + helpers): ~1,490
# Target after 5e.5 (extract main()): ~600
# Target after 5e.6 (shim only): ~50
_MAX_LOC_IN_BRIDGE = 2501


def _count_cmd_handlers() -> int:
    """Count top-level `def cmd_*` declarations in x2d_bridge.py.

    Match anchor on column 0 only — nested defs inside other functions
    don't count as new public CLI handlers."""
    src = BRIDGE.read_text(encoding="utf-8")
    return len(re.findall(r"^def cmd_", src, re.MULTILINE))


def _count_loc() -> int:
    """Total lines in x2d_bridge.py. Same number `wc -l` reports —
    counts newlines, so the last-line-without-trailing-newline edge
    case isn't double-counted."""
    return BRIDGE.read_text(encoding="utf-8").count("\n")


def test_bridge_loc_does_not_grow():
    """The monolith should only shrink. If you grew it because of a
    feature add: STOP, put the code in `beambam/<module>.py` and
    re-export from x2d_bridge if back-compat needs it."""
    actual = _count_loc()
    assert actual <= _MAX_LOC_IN_BRIDGE, (
        f"x2d_bridge.py is {actual} LoC; pinned ceiling is "
        f"{_MAX_LOC_IN_BRIDGE}. New code belongs in beambam/<module>.py — "
        "see `docs/BRIDGE_SPLIT_PLAN.md` for Phase 5e batch ordering. "
        "If you genuinely need to bypass the guard, lower the ceiling "
        "in the same commit and document why."
    )


def test_bridge_loc_pin_is_not_stale():
    """If you finished a Phase 5e batch but forgot to lower the pin,
    progress is hidden behind a stale ceiling. Allow up to 50 LoC of
    drift for incidental cleanup; beyond that, lower the ceiling."""
    actual = _count_loc()
    drift = _MAX_LOC_IN_BRIDGE - actual
    assert drift <= 50, (
        f"x2d_bridge.py is {actual} LoC but the pinned ceiling is "
        f"{_MAX_LOC_IN_BRIDGE} (drift {drift}). Lower "
        "`_MAX_LOC_IN_BRIDGE` in tests/test_bridge_split_progress.py "
        "to match — visible progress is the point of this test."
    )


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
