# Colour painting — Specification

**Status**: Complete (core); some tools are future work
**Area**: web UI (beambam.boo) + slice (daemon)
**Primary source**: `site/src/lib/print/paint.ts`, `paint3mf.ts`, `paintregion.ts`, `paintstate.ts`, `ColorPaint.svelte`, `Preview.svelte`, `site/src/routes/print/+page.svelte`; `beambam/project_sync.py` (filament append)
**Tests**: `site/src/lib/print/paint.test.ts`, `paint3mf.test.ts`, `paintregion.test.ts`, `paintstate.test.ts`; `tests/test_project_sync.py`

## Summary

Tap a 3D model on the beambam.boo print page to paint regions a chosen filament colour; the paint is
baked into the 3MF at slice time, and the daemon adds a filament for each new colour — so a
single-colour model prints multi-colour without Bambu Studio. Ported from pokefit's colorize feature.

## Motivation

MakerWorld figures are often a single colour, or the user wants to recolour parts. Doing this in the
desktop slicer and re-exporting is friction; beambam already shows the model and drives the printer, so
painting belongs there.

## How it works

Browser-side engine (no new daemon geometry endpoints):

```
tap ──(Preview.svelte raycaster)──▶ {part, triangle}
   ──(paintregion.regionAround)──▶ Set<triangle>         face / crease-region / connected-part
   ──(paintstate.PaintSession)──▶ per-triangle overrides + live preview recolour
   ──at slice (paint3mf.writePainted3mf)──▶ 3MF with paint_color baked in
   ──POST /slice with spools per colour──▶ daemon appends a filament per painted colour (project_sync)
```

- **Paint lives on the object mesh**, keyed by `(model entry path, object id, triangle index in that
  object)` — the same per-object document order `model.ts ModelScanner` loads. Every placed instance
  of an object shares one live state buffer, so painting one paints them all (as Bambu does).
- **`paint_color`** is PrusaSlicer's `TriangleSelector` serialisation; `paint.ts` decodes
  (`rootState`/`majorityState`) and encodes (`encodePaint`) whole-triangle leaves.
- **Serialisation** re-reads the original 3MF (the model XML is streamed, never kept whole), rewrites
  the painted objects' `<triangle>` elements, and re-zips; untouched triangles keep their existing
  paint byte-for-byte.
- **New colours become filaments**: the page sends a spool per colour; `project_sync.adopt_filaments`
  appends filaments beyond the project's list. Needs the daemon capability `paint-filaments`.

## Key types & functions

- `paint.ts` `encodePaint(state) / rootState(code) / majorityState(code)` — paint_color codec.
- `paintregion.ts` `regionAround(mesh, seed, mode, creaseDeg, cache?, states?)` — triangles a tap
  selects: `face` (one), `region` (flood until a crease sharper than the angle), `part` (connected
  shell), `fill` (a bucket of the tapped colour, needs `states`). The `cache` (per object) reuses the
  edge-adjacency map across taps. `PaintSession.stateAt(part, face)` backs the Pick eyedropper.
- `paintstate.ts` `PaintSession` — overrides per `(path,object)`, live buffers mutated in place (so the
  preview and plate copies share them), undo/redo/clear, `toPaintMap()`, `usedFilaments()`.
- `paint3mf.ts` `writePainted3mf(blob, paintMap)` / `paintModelXml(xml, objects)` / `hasPaint(map)`.
- `ColorPaint.svelte` — palette (file colours + "add colour"), tap modes, crease, erase, undo/redo,
  the `paint-filaments` gate; emits `PaintInfo {dirty, palette, paintMap, spools}`.
- `Preview.svelte` — `paintMode` + `onpick` (raycast on a primary tap, not a drag/pinch), `repaint()`.

## Invariants & gotchas

1. **The triangle index is the contract.** A picked face index, the index `paint3mf` writes, and the
   index `model.ts` loads back must agree — per object, document order, excluding the `<triangles>`
   container. A load→paint→serialize→reload round-trip test guards it. Mirrored placements flip winding
   but keep triangle **order**, so the index still maps.
2. **Only touched triangles go in the map**, so a file's existing split paint on untouched triangles is
   preserved (the loader decodes to a single majority state and would otherwise flatten it).
3. **Adding a colour needs the daemon** (`paint-filaments` + `slice-options`); without it the UI only
   paints the file's own colours (re-assigning regions), which needs no append.
4. **The `$effect` that builds the session must not loop**: read `model`/`fileColours` as deps, do every
   state write (and `emit()`) inside `untrack()` — Svelte re-runs an effect that reads what it writes.
5. **Spools merge, not replace**: paint spools cover appended colours; `adoptSpools` entries win for
   existing filaments. Recolouring an existing filament also sends spools so the print matches the view.
6. **Painting is project-only** (`model.kind === 'project'`): a mesh/geometry/sliced file has no object
   meshes / filament list to paint into.

## Verification

- `cd site && bun test src/lib/print/paint*.test.ts && bun ./scripts/check.ts`
- Real end-to-end (the proof): load a single-colour project, paint a region a second colour, serialize
  with `writePainted3mf`, and `slice_project` it with two spools — the result has two filaments and the
  G-code contains `M620` (see [slice-pipeline](slice-pipeline.md)). Verified 2026-11-27 on a real A1
  keychain → two filaments.
- The interactive raycast UI needs WebGL; this device's headless Chromium has none, so test tap-to-paint
  in Edge on the phone.

## Known limitations / future work

- **Large meshes**: `writePainted3mf` runs in the preview worker (`worker-client.serializePainted`), so
  re-zipping does not freeze the tab, and the edge-map is cached per object. Still unzips/re-zips the
  whole archive in memory; streaming the `.model` entry is a further optimisation.
- **Split paint**: only whole-triangle leaves are authored (no sub-triangle edges). `model_settings`
  plate-level `filament_maps` is not grown for appended filaments.
- **Multi-plate**: paint is recorded, sliced and shown on the current plate; a plate not drawn since
  painting may need a plate switch to show it (nothing is lost — the session is keyed to the whole model).
- **Tools not yet ported** from pokefit: Mirror, Grow/Shrink, the floating paint dock, and the
  field-following "wand"/brush. (Triangle, Region, Part, **Fill** and the **Pick** eyedropper are done.)

## History

- **2026-11-27** — ported (encoder → 3MF writer → region detection → session → panel → slice wiring);
  daemon-side filament append added. Fable audit → fixed an `$effect` loop, filament-array scoping, the
  retarget crash, erase/stale/multi-plate/spool-merge gaps, and added the capability gate. Verified end
  to end with a real two-colour slice.
