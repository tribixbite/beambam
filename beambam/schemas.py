"""beambam.schemas — TypedDict definitions for the wire payloads.

The Bambu MQTT protocol is loosely typed (string-encoded numbers, optional
fields scattered across firmware versions). These TypedDicts pin the
shapes that the bridge actively consumes or emits, so callers using mypy
or pyright get IDE help + type errors when the shape drifts.

All TypedDicts are `total=False` so partial payloads (mid-firmware-update
gaps, older models without certain fields) don't trigger spurious type
errors. The full set was captured from a live X2D running firmware
01.01.50.00 in 2026-05.

Usage:

    from beambam.schemas import PrintState, AmsTray
    state: PrintState = printer.state()["print"]
    bed_temp: float = state["bed_temper"]              # mypy: float

If you find a field the firmware emits that's not here, add it —
contributions welcome.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict


# ---------------------------------------------------------------------------
# AMS state
# ---------------------------------------------------------------------------


# Bambu firmware encodes lots of "numbers" as strings ("0" vs 0). Per-field
# notes call out which is which so callers don't get bit by `int(state["x"])`.


class AmsTray(TypedDict, total=False):
    """One slot in an AMS unit. Indexed by tray id ('0' through '3')."""
    id: str                                                 # "0".."3"
    tray_type: str                                          # "PLA", "PETG-CF", "PA", ...
    tray_info_idx: str                                      # "GFL99" — color barcode
    tray_color: str                                         # "RRGGBBAA" hex, no #
    cols: list[str]                                         # alternate color list (gradients)
    tray_diameter: str                                      # "1.75"
    nozzle_temp_min: str                                    # "190"
    nozzle_temp_max: str                                    # "230"
    bed_temp: str                                           # "65"
    bed_temp_type: str
    drying_temp: str
    drying_time: str
    remain: int                                             # -1 or 0..100 (%)
    state: int                                              # 11=loaded-idle, 27=feeding-active
    tag_uid: str
    total_len: int
    tray_id_name: str
    tray_sub_brands: str
    tray_uuid: str
    tray_weight: str
    xcam_info: str
    cali_idx: int
    ctype: int


class AmsDrySetting(TypedDict, total=False):
    dry_duration: int                                       # -1 if disabled
    dry_filament: str
    dry_temperature: int


class AmsUnit(TypedDict, total=False):
    """One AMS box. Multi-AMS setups (H2D / X2D) have multiple of these."""
    id: str                                                 # "0", "1", ...
    info: str                                               # firmware version hex
    humidity: str                                           # "0".."4" (relative scale)
    humidity_raw: str                                       # raw sensor reading
    temp: str                                               # "26.5" (°C)
    dry_setting: AmsDrySetting
    dry_sf_reason: list[int]
    dry_time: int
    tray: list[AmsTray]                                     # always 4 entries (some empty)


class AmsBus(TypedDict, total=False):
    """The top-level `print.ams` block — the bus connecting all AMS units."""
    ams: list[AmsUnit]                                      # one entry per AMS box
    ams_exist_bits: str
    insert_flag: bool
    power_on_flag: bool
    tray_exist_bits: str
    tray_is_bbl_bits: str
    tray_now: str                                           # active tray (global slot string)
    tray_pre: str
    tray_read_done_bits: str
    tray_reading_bits: str
    tray_tar: str
    version: int


# ---------------------------------------------------------------------------
# Print job state (the ~99-field block under `print`)
# ---------------------------------------------------------------------------


GcodeState = Literal[
    "IDLE", "PREPARE", "RUNNING", "PAUSE",
    "FINISH", "FAILED", "OFFLINE", "SLICING", "UNKNOWN",
]


class CameraState(TypedDict, total=False):
    """The `ipcam` block — chamber camera config + status."""
    ipcam_dev: str
    ipcam_record: str                                       # "enable"/"disable"
    timelapse: str                                          # "enable"/"disable"
    resolution: str                                         # "1080p","720p"
    tutk_server: str
    mode_bits: int


class PrintState(TypedDict, total=False):
    """The big one — everything under the top-level `print` key.

    Subset coverage: ~30 of 99 firmware fields. Add more as needed.
    All fields are `total=False` so old/partial firmware reports type-check.
    """
    # Identity
    command: str                                            # "push_status"
    msg: int
    sequence_id: str
    serial: str

    # Job
    gcode_state: GcodeState
    gcode_file: str
    file: str
    subtask_name: str
    job_id: str
    task_id: str
    subtask_id: str
    project_id: str
    profile_id: str
    design_id: str
    model_id: str
    lan_task_id: str
    plate_idx: int
    plate_cnt: int
    plate_id: int

    # Progress
    percent: int                                            # 0..100
    mc_percent: int
    layer_num: int
    total_layer_num: int                                    # absent in newer firmware
    mc_remaining_time: int                                  # minutes
    remain_time: int
    mc_print_stage: str
    mc_print_sub_stage: int
    mc_action: int
    print_type: str                                         # "cloud"/"local"/"system"/...
    print_error: int
    print_gcode_action: int
    print_real_action: int

    # Temperatures
    bed_temper: float
    bed_target_temper: float
    nozzle_temper: float
    nozzle_target_temper: float
    nozzle_diameter: str                                    # "0.4"
    nozzle_type: str                                        # "HS01"
    chamber_temper: float
    chamber_target_temper: float

    # Fans
    cooling_fan_speed: str
    heatbreak_fan_speed: str
    big_fan1_speed: str
    big_fan2_speed: str
    aux_part_fan: bool
    fan_gear: int

    # Filament / AMS
    ams: AmsBus
    ams_status: int                                         # mode word
    ams_rfid_status: int
    mapping: list[int]                                      # filament → tray map
    use_ams: bool

    # Camera / IPCam
    ipcam: CameraState

    # Hardware / device meta
    device: dict[str, Any]
    info: dict[str, Any]
    net: dict[str, Any]
    online: dict[str, Any]
    wifi_signal: str

    # Errors
    err: str
    err2: dict[str, Any]
    hms: list[dict[str, Any]]
    fail_reason: str
    mc_err: int
    mc_print_error_code: str
    home_flag: int

    # Lights
    lights_report: list[dict[str, Any]]

    # Other
    queue: int
    queue_est: int
    queue_number: int
    queue_sts: int
    queue_total: int
    s_obj: list[Any]
    care: list[Any]
    cfg: str
    aux: str

    # Sub-blocks (loosely typed for now — nested TypedDicts when needed)
    job: dict[str, Any]
    file_info: dict[str, Any]


class PushAllReport(TypedDict, total=False):
    """The outer wrapper around state pushes. The X2D firmware places
    everything inside a single `print` key; this matches that shape."""
    print: PrintState


# ---------------------------------------------------------------------------
# Outgoing payloads (we publish these)
# ---------------------------------------------------------------------------


class StartPrintCommand(TypedDict, total=False):
    """The inner `print` block of a project_file MQTT start_print.

    NOT signed — sign_payload() wraps the whole envelope and adds the
    `info` block with cert_id + signature. Use `from beambam.schemas
    import StartPrintCommand` to type the dict passed to
    Printer.publish({"print": <here>})."""
    sequence_id: str
    command: Literal["project_file"]
    param: str                                              # "Metadata/plate_<N>.gcode"
    project_id: str
    profile_id: str
    subtask_id: str
    task_id: str
    subtask_name: str
    job_id: str
    plate_idx: int
    md5: str
    url: str                                                # "ftp:///<file>"
    file: str
    gcode_file: str
    use_ams: bool
    ams_mapping: list[int]                                  # legacy single-int slots
    ams_mapping2: list[dict[str, int]]                      # [{"ams_id": N, "slot_id": M}, ...]
    bed_type: str                                           # "supertack_plate"/"cool_plate"/...
    bed_leveling: bool
    flow_cali: bool
    layer_inspect: bool
    nozzle_check: bool
    timelapse: bool
    print_calibration: bool


class PauseCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["pause"]


class ResumeCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["resume"]


class StopCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["stop"]


class GcodeLineCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["gcode_line"]
    param: str                                              # "G28\n"


class AmsChangeFilamentCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["ams_change_filament"]
    target: int                                             # 0..15 global slot, 255 = unload
    curr_temp: int
    tar_temp: int


class PushingCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["pushall"]


class SystemLedCommand(TypedDict, total=False):
    sequence_id: str
    command: Literal["ledctrl"]
    led_node: Literal["chamber_light", "work_light"]
    led_mode: Literal["on", "off", "flashing"]
    led_on_time: int
    led_off_time: int
    loop_times: int
    interval_time: int


# Envelopes — what publish() actually receives
class PrintEnvelope(TypedDict, total=False):
    print: dict[str, Any]                                   # any of the *Command types above

class PushingEnvelope(TypedDict, total=False):
    pushing: PushingCommand

class SystemEnvelope(TypedDict, total=False):
    system: SystemLedCommand


__all__ = [
    "AmsBus",
    "AmsUnit",
    "AmsTray",
    "AmsDrySetting",
    "CameraState",
    "GcodeState",
    "PrintState",
    "PushAllReport",
    "StartPrintCommand",
    "PauseCommand",
    "ResumeCommand",
    "StopCommand",
    "GcodeLineCommand",
    "AmsChangeFilamentCommand",
    "PushingCommand",
    "SystemLedCommand",
    "PrintEnvelope",
    "PushingEnvelope",
    "SystemEnvelope",
]
