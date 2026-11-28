# Multi-plate slicing & per-object quantity — Specification

**Status**: **Complete**
**Area**: slice, web UI
**Primary source**: `beambam/plate.py` (`extract_plate`, `plate_count`), `beambam/slice_service.py`, `beambam/slice_options.py`
**Tests**: `tests/test_plate.py`, `tests/test_slice_options.py`

## Summary

Slice one chosen plate of a multi-plate Bambu Studio project on the X2D, printing each of that plate's
objects a chosen number of times (0 to skip one), auto-arranged on the plate. This is how the print
page turns "slice just the nametag plate" or "3× of this piece, 0× of that, on one plate" into a file
the CLI slicer accepts.

## Motivation

A Bambu project can hold several plates. Bambu Studio tolerates the quirks that come with that; the CLI
slicer (bs-bionic) does not. beambam used to slice *every* plate of a project, so a multi-plate project
failed outright:

- **`code -6`** ("can not find object for assemble item") — the project's global `<assemble>` section
  (the exploded "assembly view") names objects across all plates, and the slicer aborts parsing it.
- **`code -50`** ("plate is empty or has no object fully inside it") — a plate whose objects have no
  `<build>` item (Bambu recomputes their placement on load; the slicer does not), so the plate reads as
  empty.

The per-object quantity feature used to rewrite the 3MF's `<build>` in the browser
(`site/.../quantity3mf.ts`). That only works for a single-plate project: on a multi-plate project it left
the plate metadata inconsistent and hit the same `-6`/`-50`. Quantity therefore moved to the same
single-plate extraction path, on the daemon.

## How it works

```
upload (paint baked in) + X-Slice-Options {plate, quantity, nozzles, bed_type, spools}
        │
        ▼  slice_service.slice_project
   plate_count(src) > 1  OR  opts.plate/opts.quantity set
        │
        ▼  plate.extract_plate(src, single, opts.plate or 1, opts.quantity)
   one clean single-plate 3MF  ──▶  the normal (retarget / project) slice path
```

The web UI (`site/src/routes/print/+page.svelte`) computes the per-plate pieces
(`pieces`, scoped to the selected plate via `partsOnPlate`), packs the wanted copies client-side for the
live preview (`arrange.ts` `shelfPack` → `displayModel`), and sends the result as slice options
(`buildSliceOptions(..., plateToSlice, quantityRecord)` → `optionsHeader`). It no longer rewrites the
3MF itself. The daemon does the authoritative rewrite in `extract_plate`.

## Key types & functions

- `plate.plate_count(src) -> int` — plates in the project (1 for a mesh or a one-plate project).
- `plate.extract_plate(src, dst, plate=1, spec=None) -> {objects, copies}` — write a single-plate copy:
  - keeps only that plate's `<object>` blocks and one `<plate>` (renumbered to 1, preview metadata
    dropped), and **removes the `<assemble>` section**;
  - rewrites `<build>` to that plate's items; an object with no build item is dropped (so it cannot make
    the plate "empty");
  - applies `spec` (`{object id: [(dx, dy), …]}`, one offset per wanted copy, `[]` drops the object):
    each copy is a `<build>` item translated by its offset with a fresh `p:UUID`, plus a matching
    `<model_instance>`;
  - renames this plate's `Metadata/{plate,top,pick,plate_no_light}_<N>.<ext>` to `*_1` and drops the
    other plates'.
- `SliceOptions.plate: int | None`, `SliceOptions.quantity: {oid: ((dx,dy),…)} | None` — the slice
  options carrying the selection (`slice_options.py`; validated by `_quantity`).
- Web `SliceOptions.plate` / `.quantity` and `buildSliceOptions(settings, mesh, nozzles, spools, plate,
  quantity)` (`site/src/lib/print/plan.ts`).

## Invariants & gotchas

1. **The spec's object ids are the root build-item object ids**, which the web UI exposes as
   `PreviewPart.buildObjectId` (Bambu wraps a mesh in an assembly object, so the build-item id is **not**
   the mesh object id). Key the spec by `buildObjectId`, or the daemon matches nothing and the plate is
   sliced unchanged.
2. **The `<assemble>` section must be stripped**, not fixed. Its items reference objects across plates;
   on a single extracted plate they cannot resolve, and the slicer aborts with `-6`. It is only the
   assembly view and is never needed to slice.
3. **A build-item offset moves the translation at transform indices 9 (tx) and 10 (ty)**; the first nine
   terms are rotation/scale. The copies' `(dx, dy)` come from the browser packer in the same plate-mm
   space as the item translation, so adding them lands each copy at its packed centre.
4. **An object on the plate with no `<build>` item is dropped** (its model_instance alone has no
   geometry). This is what makes an otherwise-malformed plate sliceable; its symptom if *not* dropped is
   `-50`.
5. **Meshes (STL/OBJ) have no plates** — `slice_service` only extracts when `is_mesh(src)` is false.
6. **Changing the selected plate must re-slice**: the web `settingsKey` includes `plateToSlice`, and the
   quantity preview/spec are scoped to that plate's parts.

## Verification

Unit: `python3 -m pytest tests/test_plate.py tests/test_slice_options.py`.

End to end (needs the on-device slicer, bs-bionic): extract and slice a real multi-plate project and
confirm the right objects, `code 0`, and the AMS-load step:

```python
from pathlib import Path
from beambam.slice_service import slice_project
from beambam.slice_options import SliceOptions
# 3× of object 4, drop object 9, on plate 2, as the UI would send it:
o = SliceOptions(plate=2, quantity={"4": ((0,0),(28,0),(56,0)), "9": ()})
r = slice_project(Path("project.3mf"), Path("out"), options=o, track_switch=True)
assert r["ok"] and r["plates"] == 1          # one plate, ~3× the grams of one copy
```

A pure extraction check: `extract_plate(project, out, 3)` then slice `out` with `SliceOptions()` and look
for `M620` (AMS load) in `out/Metadata/plate_1.gcode`.

## Known limitations / future work

- An object that a plate places but that has **no `<build>` item** is dropped, not recovered — its plate
  position is not stored in a form the extractor can read (only in the exploded assemble coordinates). A
  project whose wanted object is build-item-less on its plate loses that object. (Most projects give every
  placed object a build item; the ones that don't are the same ones that break stock bs-bionic.)
- Quantity arranges with the browser's greedy shelf packer (`arrange.ts`); it does not rotate pieces or
  pack around a brim beyond a fixed margin.
- "Separate pieces" are whole objects/build items, not disconnected mesh islands inside one object.
