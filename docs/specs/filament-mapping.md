# Filament mapping & project-sync — Specification

**Status**: Complete
**Area**: slice + print (daemon)
**Primary source**: `beambam/project_sync.py`, `beambam/filament_mapping.py`, `beambam/slice_options.py`
**Tests**: `tests/test_project_sync.py`, `tests/test_filament_profiles.py`, `tests/test_slice_options.py`

## Summary

Make a file's filaments slice and print with the right presets, colours, nozzles and AMS slots:
rewrite a project's filament list from the chosen spools ("project-sync"), append filaments for painted
colours, and map each filament to an AMS slot / nozzle for the print command.

## How it works

Two stages:

1. **Slice-time (`project_sync`)**: `adopt_filaments(settings, profiles, spools)` sets project filament
   `i` to `spools[i]` — the bundled system preset for the spool's filament id/material, and the spool's
   colour. Spools **past** the project's filament count **append** filaments (a painted new colour), each
   cloned from the last then given the spool's colour/preset. `adopt_project(src, dst, …)` writes a new
   3MF with the adopted settings. `apply_to_project` wires it into `/slice` and `/project/update`.
2. **Print-time (`filament_mapping`)**: compute `ams_mapping` (filament-indexed global slots) and
   `ams_mapping2` (`[{ams_id, slot_id}]`) from live AMS state, and the per-filament nozzle (`group_id` /
   extruder, left/right) with the X2D's Filament Track Switch. Plate markers (e.g. SuperTack `P0301`)
   live in `PLATE_MARKERS`.

## Key types & functions

- `project_sync.adopt_filaments(settings, profiles, spools, warnings=None) -> dict` — the core rewrite.
  Appends when `len(spools) > n`.
- `project_sync._grow_filaments(out, n, m)` — append `m-n` filaments by cloning the last, growing only
  per-filament arrays.
- `project_sync.FILAMENT_KEYS` — the allowlist of genuine per-filament project keys (derived from real
  X2D/A1/P2S projects). Growth = `key in FILAMENT_KEYS or key in LAYOUT_KEYS or key.startswith("filament_")`.
- `project_sync._flush_matrix(mat, old_colours, new_colours)` — rebuild `flush_volumes_matrix` to the
  new colour count (growth-aware; a default purge when the source has no off-diagonal data).
- `slice_options.SliceOptions.spools` / `filament(i)` — the per-filament (id, material, colour) sent as
  `X-Slice-Options`.

## Invariants & gotchas

1. **Grow only real filament keys.** A one-filament project divides every machine/process list by one,
   so "length divisible by the filament count" grows `nozzle_diameter`, `printable_area`,
   `machine_max_*` etc. and corrupts the project (bs refuses it or the print is wrong). Use
   `FILAMENT_KEYS`. *Symptom if wrong: "filament_is_support's count 2 not equal to filament_colour's
   size 1" (code -5), or a nozzle/printable-area mismatch.*
2. **`filament_self_index` renumbers** in whole-filament blocks; the `n+2` bookkeeping arrays
   (`different_settings_to_system`, `inherits_group`) grow by inserting after the last filament.
3. **The flush matrix is read against the original colour count** (`n_orig`) and rebuilt to the new one;
   indexing `old_colours` with a new index overflows.
4. **Retargeted projects append in two places**: `adopt_filaments` grows the *embedded* project (via
   `adopt_project` in the retarget path) AND `mesh_args` loads that many filament profiles. See
   [slice-pipeline](slice-pipeline.md).
5. **State k ↔ `spools[k-1]` ↔ appended position k-1** — the painted `paint_color` state (1-based
   filament number) must line up with the appended filament. No off-by-one.

## Verification

- `pytest tests/test_project_sync.py` — append grows every per-filament array consistently, leaves
  machine arrays alone, flush matrix sized correctly.
- On real data: grow a real project's `project_settings` 1→2 and assert no machine key changed and every
  filament key grew (see the `adopt_filaments` tests).

## History

- **2026-11-27** — `adopt_filaments` learned to append (was: spools past the project were ignored);
  `FILAMENT_KEYS` allowlist added after a real slice showed the length-divisible heuristic corrupted a
  one-filament project; flush matrix made growth-aware.
