/**
 * Shelf bin-packing for the per-object quantity feature: place N copies of each object's footprint on the
 * build plate without overlap, leaving a margin (brim + clearance) around each. Returns a target centre for
 * every copy and whether they all fit, so the UI can warn before slicing.
 */
export interface Rect {
    /** Footprint width / depth (model units). */
    w: number;
    h: number;
}
export interface Placement {
    /** Target footprint centre in plate coordinates. */
    cx: number;
    cy: number;
}
export interface Bed {
    minX: number;
    minY: number;
    maxX: number;
    maxY: number;
}

export interface PackResult {
    places: Placement[];
    fits: boolean;
}

/**
 * Place `rects` (one per copy, in the caller's order) on `bed`, each padded by `margin` on every side.
 * Greedy shelf packer: tallest first, filled left→right in rows that stack front→back. `fits` is false when a
 * rectangle would cross the bed's back or right edge (the UI warns; the placements are still returned).
 */
export function shelfPack(rects: Rect[], bed: Bed, margin = 5): PackResult {
    const bedW = bed.maxX - bed.minX;
    const bedH = bed.maxY - bed.minY;
    // Tallest first makes shelves pack tighter; keep each rect's original index for the result.
    const order = rects.map((_, i) => i).sort((a, b) => rects[b].h - rects[a].h);
    const places: Placement[] = new Array(rects.length);
    let x = margin;
    let y = margin;
    let shelfH = 0;
    let fits = true;
    for (const i of order) {
        const w = rects[i].w + 2 * margin;
        const h = rects[i].h + 2 * margin;
        if (x + w > bedW && x > margin) {
            // next shelf
            x = margin;
            y += shelfH;
            shelfH = 0;
        }
        if (x + w > bedW + 1e-6 || y + h > bedH + 1e-6) fits = false;
        places[i] = { cx: bed.minX + x + w / 2, cy: bed.minY + y + h / 2 };
        x += w;
        if (h > shelfH) shelfH = h;
    }
    return { places, fits };
}
