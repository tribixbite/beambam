# Color painting on beambam.boo — port plan

Porting Pokefit's tap-to-paint colour feature (`web/src/colorize.ts`,
`paintdock.ts`, `scene.ts` picking) to the beambam.boo `/print` page, so a
single-colour model can be painted into a multi-colour print without Bambu
Studio.

**Decisions (locked 2026-11-27):**
- **Engine: browser / client-side.** No new daemon geometry endpoints; the
  page does the picking, region detection and 3MF rewrite itself and sends the
  painted file to the existing `/slice`. (Pokefit's "browser" mode.)
- **Whole-triangle leaves first.** Region/Part painting colours whole
  triangles, which is the common case and avoids split-triangle geometry.
  Split (partial-triangle) paint is a later follow-up.
- **Paint lives on the object mesh, not the instance.** A `paint_color` is on
  the `<object>`'s `<triangle>`, so every build instance of that object shows
  the paint (matches Bambu). The paint map is keyed by **(model file path,
  objectId, triangle index within that object)** — ModelScanner scans
  triangles in document order per object, so the index is stable.

## Data flow

Load (`load.ts`) streams the 3MF and keeps only preview geometry — the raw
model XML is **not** retained (files reach 172 MB). So paint is re-injected by
re-reading the original `File` and rewriting the `.model` entries, then
re-zipping, at slice time.

```
pick (three.js Raycaster on Preview)            →  {part, objectId, triIndex, point}
region detect (crease / connected / single)     →  Set<triIndex>  (pure geom)
paint state map  (objectId → triIndex → filament)→  shown live in Preview (recolor)
serialize at slice (paint3mf)                    →  new 3MF Blob with paint_color
POST /slice (daemon)                             →  multi-colour sliced file
```

## Increments

1. **✅ `paint_color` encoder** (`paint.ts` `encodePaint`, tests) — commit
   `b474557`. Inverse of the decoder, whole-triangle leaves, states 1..40
   round-trip.
2. **`paint3mf.ts` — 3MF paint writer.** `(original Blob, paintMap) → Blob`:
   re-read every entry (fflate `Zip`/`zipSync`, already a dep), rewrite each
   `.model`'s `<triangle …/>` to carry `paint_color` for painted indices
   (strip an existing one when repainting), re-zip. Pure; test with a small
   synthetic 3MF + round-trip through `loadModel`. *De-risks serialization —
   build next.*
3. **Scene picking** (`Preview.svelte`). Add a `THREE.Raycaster` on
   `pointerdown`; map the hit face to (part → objectId, triIndex); emit an
   `onpick` callback. Keep orbit-drag vs tap apart (movement threshold).
4. **Region detection** (`paintregion.ts`, pure geom + tests). Modes: `single`
   (one triangle), `region` (flood across shared edges while the dihedral angle
   ≤ crease), `part` (whole connected component). Needs per-object edge
   adjacency from `RawMesh`.
5. **`ColorPaint.svelte` panel.** Palette from the file's filaments + "add
   colour" (new filament), tap-mode selector, live preview recolor, Undo/Redo.
   New paint adds filaments, which flow into the existing MappingPanel (step
   06) as spool rows.
6. **Wire into slice.** When any paint exists: serialize via `paint3mf` and
   send the painted Blob to `/slice`; a paint change marks the slice stale; the
   added filaments appear in Spools. Guard: don't rewrite when nothing painted.
7. **Follow-ups** (later): bucket Fill (bounded by colour), Mirror, Pick
   (eyedropper), Grow/Shrink, the floating paint dock, and split-triangle paint
   for sub-triangle edges.

## Status (2026-11-27) — shipped and verified

Increments 1–6 are done, plus the daemon-side filament append and the fixes
from a Fable audit. **Verified end to end**: a single-colour A1 keychain
painted with a second colour slices to a valid two-filament X2D print
(1.38 g + 2.58 g) through the retarget path. All paint lib tests (bun) and
`test_project_sync` (pytest) pass; the site builds.

Daemon: `project_sync._grow_filaments` appends filaments scoped to real
per-filament keys (`FILAMENT_KEYS` allowlist); the retarget path grows the
embedded project and keeps each filament's material; `_flush_matrix` is
growth-aware with a default purge; `/healthz` advertises `paint-filaments`.

### Known limitations (future work)
- **Large meshes**: `writePainted3mf` unzips/re-zips and rewrites the model XML
  as one string on the main thread — fine for figures, but a 1.9M-triangle
  model would freeze the tab. Move it (and the edge-map build, now cached per
  object) into the preview worker and stream the `.model` entry.
- **Multi-plate live display**: the paint is recorded, sliced and shown on the
  current plate, but a plate the preview has not drawn since painting may need a
  plate switch to show it (the session is keyed to the whole model, so nothing
  is lost).
- **Split paint**: only whole-triangle leaves are authored; a file's existing
  split paint on untouched triangles is preserved, but painting always writes
  whole triangles (no sub-triangle edges), and `model_settings.config`
  plate-level `filament_maps` is not grown for appended filaments.
- Triangles written as `<triangle …></triangle>` (not self-closing) are not
  matched by the writer; Bambu/Orca/PrusaSlicer always self-close them.

## Notes / risks

- **Triangle-index contract** is the linchpin: the writer in step 2 must count
  `<triangle>` per `<object>` in the same document order ModelScanner uses.
  Lock it with a load→paint→serialize→reload round-trip test.
- **Big files**: the rewrite streams the `.model` entry; never hold the whole
  XML as one string for a 172 MB mesh (match load.ts's chunked approach).
- STL/OBJ meshes become a one-object 3MF on load, so painting works the same
  once they carry an objectId.
