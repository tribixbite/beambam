/**
 * Which triangles a paint tap covers, from the mesh geometry alone (no engine).
 *
 * Modes (ported from the whole-triangle subset of pokefit/colorize.py):
 *   - `face`   — just the tapped triangle.
 *   - `region` — flood across shared edges while the crease between faces is softer than the
 *                angle (stop at any crease sharper than it): one smooth patch up to the nearest edge.
 *   - `part`   — every triangle connected to the tapped one (a whole shell).
 *
 * Paint targets 3MF models, whose triangles share indexed vertices, so an edge is a shared pair of
 * vertex indices. Split-triangle (sub-triangle) paint and the field-following "wand"/brush modes are
 * later follow-ups (see docs/specs/colour-painting.md).
 */
export type PaintMode = 'face' | 'region' | 'part';

/** The three undirected edges of triangle `t`, each as the ordered vertex-index pair. */
function edgesOf(indices: Uint32Array, t: number): [number, number][] {
    const a = indices[t * 3];
    const b = indices[t * 3 + 1];
    const c = indices[t * 3 + 2];
    const e = (x: number, y: number): [number, number] => (x < y ? [x, y] : [y, x]);
    return [e(a, b), e(b, c), e(c, a)];
}

/** Triangles sharing each edge: key `min*V + max` -> triangle indices. */
function edgeMap(indices: Uint32Array, triCount: number, vCount: number): Map<number, number[]> {
    const map = new Map<number, number[]>();
    for (let t = 0; t < triCount; t++) {
        for (const [x, y] of edgesOf(indices, t)) {
            const key = x * vCount + y;
            const list = map.get(key);
            if (list) list.push(t);
            else map.set(key, [t]);
        }
    }
    return map;
}

/** Unit normal of triangle `t` (zero vector for a degenerate triangle). */
export function triangleNormal(positions: Float32Array, indices: Uint32Array, t: number): [number, number, number] {
    const ia = indices[t * 3] * 3;
    const ib = indices[t * 3 + 1] * 3;
    const ic = indices[t * 3 + 2] * 3;
    const ux = positions[ib] - positions[ia];
    const uy = positions[ib + 1] - positions[ia + 1];
    const uz = positions[ib + 2] - positions[ia + 2];
    const vx = positions[ic] - positions[ia];
    const vy = positions[ic + 1] - positions[ia + 1];
    const vz = positions[ic + 2] - positions[ia + 2];
    const nx = uy * vz - uz * vy;
    const ny = uz * vx - ux * vz;
    const nz = ux * vy - uy * vx;
    const len = Math.hypot(nx, ny, nz) || 1;
    return [nx / len, ny / len, nz / len];
}

export interface RegionMesh {
    positions: Float32Array;
    /** Index triples; required (3MF geometry is indexed). */
    indices: Uint32Array;
}

/** Reusable per-mesh index so a repeated tap on the same object does not rebuild the edge map. */
export interface RegionCache {
    edges?: Map<number, number[]>;
}

/**
 * The set of triangle indices a tap on `seed` selects, for `mode`. `creaseDeg` is the crease angle
 * for `region` (a larger angle floods further; default 30°, pokefit's "region" default). Pass a `cache`
 * (one per object) to reuse the edge map across taps — it is built once and stored there.
 */
export function regionAround(mesh: RegionMesh, seed: number, mode: PaintMode, creaseDeg = 30, cache?: RegionCache): Set<number> {
    const { positions, indices } = mesh;
    const triCount = (indices.length / 3) | 0;
    const out = new Set<number>();
    if (seed < 0 || seed >= triCount) return out;
    out.add(seed);
    if (mode === 'face') return out;

    const vCount = positions.length / 3;
    let edges = cache?.edges;
    if (!edges) {
        edges = edgeMap(indices, triCount, vCount);
        if (cache) cache.edges = edges;
    }
    const cosLimit = Math.cos((Math.max(0, Math.min(180, creaseDeg)) * Math.PI) / 180);
    const normals: ([number, number, number] | null)[] = mode === 'region' ? new Array(triCount).fill(null) : [];
    const normal = (t: number): [number, number, number] => {
        let n = normals[t];
        if (!n) {
            n = triangleNormal(positions, indices, t);
            normals[t] = n;
        }
        return n;
    };

    const stack = [seed];
    while (stack.length) {
        const t = stack.pop() as number;
        for (const [x, y] of edgesOf(indices, t)) {
            for (const nb of edges.get(x * vCount + y) ?? []) {
                if (nb === t || out.has(nb)) continue;
                if (mode === 'region') {
                    const a = normal(t);
                    const b = normal(nb);
                    // A degenerate triangle has a zero normal; treat it as coplanar (cross it) rather than a
                    // crease, so a sliver does not wall off the region.
                    const degenerate = (!a[0] && !a[1] && !a[2]) || (!b[0] && !b[1] && !b[2]);
                    if (!degenerate && a[0] * b[0] + a[1] * b[1] + a[2] * b[2] < cosLimit) continue; // crease: stop
                }
                out.add(nb);
                stack.push(nb);
            }
        }
    }
    return out;
}
