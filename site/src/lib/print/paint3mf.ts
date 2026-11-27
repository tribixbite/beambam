/**
 * Write per-triangle paint back into a 3MF, client-side, for the colour-painting feature.
 *
 * The model is loaded as preview geometry only (load.ts keeps no raw XML), so paint chosen on
 * the preview is re-injected by re-reading the original archive and rewriting the `<triangle>`
 * elements of the painted objects, then re-zipping. The daemon then slices the painted file.
 *
 * Contract (must match ModelScanner's indexing in model.ts): inside each `<object>`'s mesh,
 * triangles are counted in document order; the paint map keys them by that index. Paint lives on
 * the object's mesh, so every build instance of the object shows it (as Bambu does). Only whole
 * triangles are painted here (one `paint_color` leaf); split paint is a later follow-up.
 *
 * See docs/specs/colour-painting.md.
 */
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import { attr } from './model';
import { encodePaint } from './paint';

/** Filament state per triangle index within one object (state 0 clears any existing paint). */
export type ObjectPaint = Map<number, number>;
/** objectId -> that object's triangle paint. */
export type ModelPaint = Map<string, ObjectPaint>;
/** Model entry path (e.g. `3D/3dmodel.model`) -> its objects' paint. */
export type PaintMap = Map<string, ModelPaint>;

const OBJECT_OR_TRIANGLE = /<object\b[^>]*>|<triangle\b[^>]*?\/>/g;
const PAINT_ATTR = /\s+(?:paint_color|slic3rpe:mmu_segmentation)="[^"]*"/g;

/** True when the map records at least one triangle edit (including an erase to state 0). */
export function hasPaint(paint: PaintMap | null | undefined): boolean {
    if (!paint) return false;
    for (const objects of paint.values()) for (const tris of objects.values()) if (tris.size) return true;
    return false;
}

/** One `<triangle …/>` tag with its paint set to `state` (0 removes paint). */
function setTrianglePaint(tag: string, state: number): string {
    const bare = tag.replace(PAINT_ATTR, '');
    const code = encodePaint(state);
    if (!code) return bare;
    // Insert before the self-closing `/>` (keep any trailing space tidy).
    return bare.replace(/\s*\/>$/, ` paint_color="${code}"/>`);
}

/**
 * Rewrite one `.model` file's XML so the triangles named in `objects` carry the chosen paint.
 * Triangles not in the map keep whatever paint they already had.
 */
export function paintModelXml(xml: string, objects: ModelPaint): string {
    let currentId = '';
    let triIndex = 0;
    OBJECT_OR_TRIANGLE.lastIndex = 0;
    return xml.replace(OBJECT_OR_TRIANGLE, (tag) => {
        if (tag[1] === 'o') {
            // <object …>: switch to it and restart its triangle count.
            currentId = attr(tag, 'id') ?? '';
            triIndex = 0;
            return tag;
        }
        // <triangle …/>
        const idx = triIndex++;
        const state = objects.get(currentId)?.get(idx);
        return state === undefined ? tag : setTrianglePaint(tag, state);
    });
}

/**
 * A copy of `original` with the paint applied. Entries not in `paint` are passed through
 * unchanged. Returns the original Blob when nothing is painted.
 *
 * v1 holds the archive in memory (unzip all → rewrite → re-zip); fine for the small figures
 * painting targets. Streaming the big `.model` entry is a follow-up (see docs/specs/colour-painting.md).
 */
export async function writePainted3mf(original: Blob, paint: PaintMap): Promise<Blob> {
    if (!hasPaint(paint)) return original;
    const buf = new Uint8Array(await original.arrayBuffer());
    const entries = unzipSync(buf);
    for (const [path, objects] of paint) {
        const data = entries[path];
        if (!data) throw new Error(`painted model entry ${path} is not in the 3MF`);
        entries[path] = strToU8(paintModelXml(strFromU8(data), objects));
    }
    const out = zipSync(entries, { level: 6 });
    return new Blob([out as BlobPart], { type: 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml' });
}
