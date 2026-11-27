# Slice pipeline — Specification

**Status**: Complete
**Area**: slice (daemon)
**Primary source**: `beambam/slice_service.py`, `beambam/slice_options.py`, `beambam/project_sync.py`, `beambam/profile_flatten.py`
**Tests**: `tests/test_slice_options.py`, `tests/test_project_sync.py`, `tests/test_slice_multi.py`, `tests/test_profile_flatten.py`, `tests/test_slice_real.py` (gated by `BEAMBAM_REAL_SLICER=1`)

## Summary

Slice a user's 3MF / STL / OBJ into an X2D print file on-device with the bundled Bambu Studio build
(`bs-bionic`), re-targeting projects made for other printers to the X2D, applying the user's settings
and the chosen spools' filaments, and producing G-code that actually loads filament and prints.

## Motivation

Users bring MakerWorld files made for an A1/P2S/X1C; beambam prints them on the X2D without the cloud
or the desktop slicer. The CLI is headless and particular about profiles, so most of the complexity
is feeding it a valid, complete machine/process/filament configuration.

## How it works

```
file ──▶ slice_project(src, out_dir, options, track_switch)      (slice_service.py)
          ├─ resolve_slicer(): find bs-bionic; flatten profiles if stale/missing (profile_flatten)
          ├─ is it a mesh?  ── yes ▶ mesh_args(): --load-settings machine_full;process + --load-filaments
          ├─ made_for_another_printer()? ── yes ▶ RETARGET:
          │     retarget(settings, opts) + mesh_args(..., keep=settings); grow embedded project if a
          │     painted colour needs a new filament (adopt_project)
          └─ native X2D project ── apply_to_project(opts) + --load-settings machine_profile()
          ▶ bs-bionic --slice 0 --export-3mf  ▶  <stem>.gcode.3mf  (+ per-plate plate_N.gcode)
```

Three input shapes, three paths:
1. **Mesh** (STL/OBJ/geometry-only 3MF): sliced for `MESH_PRINTER` ("Bambu Lab X2D 0.4 nozzle") with
   the bundled profiles; the filament comes from the chosen spool's preset.
2. **Project made for another printer** (the common MakerWorld case): the `retarget` path. The model,
   paint and filament assignment are kept; it is sliced with the X2D's own machine/process profiles
   (its own printer profile would make a file for the wrong printer).
3. **Native X2D project**: keeps its own settings, with the user's `options` overlaid
   (`apply_to_project` → `project_sync`).

### Profiles (`profile_flatten.py`)

The bs-bionic CLI loads system presets **by name** from `resources/profiles/BBL/{process,filament,machine}_full/`.
A source checkout (the phone build) lacks those `*_full` folders, so `profile_flatten.flatten()` writes
them by resolving each profile's **`inherits`** (parent) **and `include`** (templates). Bambu splits a
machine's start/end/layer-change/change-filament G-code into `"<name> template <section>"` files the
machine profile only *includes* — so without resolving `include`, the flattened machine profile has a
bare start G-code. `FLATTEN_VERSION` + a `.flatten_version` stamp let `missing()` force a rebuild when
the flattener changes (not only when a folder is absent).

## Key types & functions

- `slice_service.slice_project(src, out_dir, *, options, track_switch) -> dict` — the entry point.
  Returns `{ok, file, seconds, grams, grams_per_filament, warnings, error}`; slicer failures come back
  as `error`, never raised.
- `slice_service.machine_profile(settings, profiles)` and `slice_options.machine_file(profiles, name)`
  — resolve the machine profile to pass to `--load-settings`, **preferring the flattened `machine_full`
  copy**. Loading the raw `machine/` profile by path does not get its `include` templates resolved.
- `slice_options.retarget(settings, opts)` — options for slicing another printer's project on the X2D;
  fills a painted spool's null id/type from the project so materials are kept (not reset to PLA).
- `slice_options.mesh_args(profiles, opts, out_dir, n_filaments, keep, arrange)` — the CLI args for a
  mesh or a retargeted project. Sizes the filament count to `max(n_filaments, len(spools), …)`.
- `project_sync.adopt_filaments` / `adopt_project` — set a project's filaments to the spools'
  presets+colours, and **append** filaments when there are more spools than the project's filaments
  (a painted new colour). See [filament-mapping](filament-mapping.md).
- `profile_flatten.flatten(profiles)` / `missing(profiles)` — build/report the `*_full` folders;
  `FLATTEN_VERSION` forces a rebuild after a flattener change.

## Invariants & gotchas

1. **The sliced G-code must load filament (`M620`).** The machine start G-code must be the real X2D
   one (bed mesh `G29`, AMS load `M620`, purge). *Symptom if wrong: the printer homes and moves but
   extrudes nothing — "printed air".* Root cause seen 2026-11-27: `profile_flatten` ignored `include`
   AND the slice loaded the raw `machine/` profile, so every slice had a bare start G-code.
2. **Load `machine_full`, not `machine`.** bs-bionic does not resolve a profile's `include` when the
   profile is given to `--load-settings` by path. Use `machine_file()`.
3. **`FLATTEN_VERSION` must be bumped** whenever `_flatten_kind`/`resolve` changes, or daemons keep a
   stale `*_full`.
4. **Appending filaments must not corrupt the project** — grow only `project_sync.FILAMENT_KEYS` (+
   `filament_*` + LAYOUT_KEYS), never every list whose length divides by the filament count.
5. **A retargeted project's embedded `project_settings` must be grown too** when a painted colour adds
   a filament, or bs-bionic refuses it ("filament_colour's size N not equal to …").
6. **Headless bs-bionic resets `has_filament_switcher` / `enable_filament_dynamic_map` to false**
   unless passed as CLI options; the grouping mode comes from the **plate** before the project. See
   `CLAUDE.md`.

## Verification

- Unit: `pytest tests/test_slice_options.py tests/test_project_sync.py tests/test_profile_flatten.py tests/test_slice_multi.py`.
- **Real slice (the one that matters):** slice a real project and assert the G-code loads filament:
  ```python
  from beambam.slice_service import slice_project; from beambam.slice_options import SliceOptions
  res = slice_project(Path("some.3mf"), out, track_switch=True)
  data = open(out/"plate_1.gcode").read()
  assert "M620" in data and "G29" in data[:30000]   # AMS load + bed mesh present
  ```
  A single-colour A1 project painted with a second colour must slice to two filaments whose G-code
  contains `M620`.

## Known limitations / future work

- Flattening the full filament set (1572 profiles) is slow on first run; it is cached in `*_full`.
- Plate-level `filament_maps` in `model_settings.config` is not grown for appended filaments.

## History

- **2026-11-27** — air-print root cause: `profile_flatten.resolve()` ignored `include`, and the slice
  loaded the raw `machine/` profile; both fixed, `FLATTEN_VERSION=2` forces a rebuild. Verified with a
  real slice (M620 present). Earlier the same day: `_grow_filaments` scoped to `FILAMENT_KEYS`;
  retarget path grows the embedded project and keeps materials.
