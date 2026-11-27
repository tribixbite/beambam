/**
 * Which triangles a paint tap covers, from the mesh geometry alone (no engine).
 *
 * Modes (ported from the whole-triangle subset of pokefit/colorize.py):
 *   - `face`   — just the tapped triangle.
 *   - `region` — flood across shared edges while the crease between faces is softer than the
 *                angle (stop at any crease sharper than it): one smooth patch up to the nearest edge.
 *   - `part`   — every triangle connected to the tapped one (a whole shell).
 *   - `fill`   — a bucket fill: triangles connected to the tapped one that currently show the SAME
 *                colour (needs the per-triangle state array).
 *   - `brush`  — the connected surface within `brushRadius` (model units) of the tapped triangle.
 *
 * Paint targets 3MF models, whose triangles share indexed vertices, so an edge is a shared pair of
 * vertex indices. Split-triangle (sub-triangle) paint and the field-following "wand" are later
 * follow-ups (see docs/specs/colour-painting.md).
 */
export type PaintMode = 'face' | 'region' | 'part' | 'fill' | 'brush';

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
 * (one per object) to reuse the edge map across taps — it is built once and stored there. `states` is the
 * per-triangle colour state, required for `fill` (a bucket of one colour).
 */
export function regionAround(
    mesh: RegionMesh,
    seed: number,
    mode: PaintMode,
    creaseDeg = 30,
    cache?: RegionCache,
    states?: Uint8Array,
    brushRadius = 5
): Set<number> {
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
    const fillState = mode === 'fill' && states ? states[seed] : -1;
    const brushCentre = mode === 'brush' ? centroid(positions, indices, seed) : null;
    const brushR2 = brushRadius * brushRadius;
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
                if (mode === 'fill') {
                    if (!states || states[nb] !== fillState) continue; // bucket: stop at a colour change
                } else if (mode === 'brush') {
                    if (brushCentre) {
                        const c = centroid(positions, indices, nb);
                        const d2 = (c[0] - brushCentre[0]) ** 2 + (c[1] - brushCentre[1]) ** 2 + (c[2] - brushCentre[2]) ** 2;
                        if (d2 > brushR2) continue; // outside the brush radius
                    }
                } else if (mode === 'region') {
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

/** The edge map for a mesh, from the cache or freshly built (and stored in the cache). */
function getEdges(mesh: RegionMesh, cache?: RegionCache): Map<number, number[]> {
    if (cache?.edges) return cache.edges;
    const edges = edgeMap(mesh.indices, (mesh.indices.length / 3) | 0, mesh.positions.length / 3);
    if (cache) cache.edges = edges;
    return edges;
}

/** Triangles sharing an edge with triangle `t`. */
function neighbours(mesh: RegionMesh, edges: Map<number, number[]>, t: number): number[] {
    const vCount = mesh.positions.length / 3;
    const out: number[] = [];
    for (const [x, y] of edgesOf(mesh.indices, t)) {
        for (const nb of edges.get(x * vCount + y) ?? []) if (nb !== t) out.push(nb);
    }
    return out;
}

/** Triangles adjacent to `tris` but not in it (the ring that Grow adds). */
export function ringAround(mesh: RegionMesh, tris: Set<number>, cache?: RegionCache): Set<number> {
    const edges = getEdges(mesh, cache);
    const ring = new Set<number>();
    for (const t of tris) for (const nb of neighbours(mesh, edges, t)) if (!tris.has(nb)) ring.add(nb);
    return ring;
}

/** Triangles in `tris` that touch one outside it (the boundary ring that Shrink removes). */
export function boundaryOf(mesh: RegionMesh, tris: Set<number>, cache?: RegionCache): Set<number> {
    const edges = getEdges(mesh, cache);
    const edge = new Set<number>();
    for (const t of tris) {
        for (const nb of neighbours(mesh, edges, t)) {
            if (!tris.has(nb)) {
                edge.add(t);
                break;
            }
        }
    }
    return edge;
}

/** Centroid of triangle `t`. */
function centroid(positions: Float32Array, indices: Uint32Array, t: number): [number, number, number] {
    const a = indices[t * 3] * 3;
    const b = indices[t * 3 + 1] * 3;
    const c = indices[t * 3 + 2] * 3;
    return [
        (positions[a] + positions[b] + positions[c]) / 3,
        (positions[a + 1] + positions[b + 1] + positions[c + 1]) / 3,
        (positions[a + 2] + positions[b + 2] + positions[c + 2]) / 3
    ];
}

/**
 * For each triangle in `tris`, the triangle whose centroid is nearest the mirror of its centroid across
 * the plane `axis = planeAt` (axis 0 = X, the usual left/right symmetry). Used by Mirror to paint the
 * symmetric spot. Triangles with no reasonably close mirror (further than `tol`) are skipped — an
 * asymmetric model simply mirrors the parts that do match.
 */
export function mirrorTriangles(mesh: RegionMesh, tris: Iterable<number>, axis = 0, planeAt?: number, tol?: number): Set<number> {
    const { positions, indices } = mesh;
    const triCount = (indices.length / 3) | 0;
    // Plane position: the bounding-box centre on `axis` unless given.
    let lo = Infinity;
    let hi = -Infinity;
    for (let i = axis; i < positions.length; i += 3) {
        if (positions[i] < lo) lo = positions[i];
        if (positions[i] > hi) hi = positions[i];
    }
    const plane = planeAt ?? (lo + hi) / 2;
    const limit = (tol ?? (hi - lo) * 0.02) ** 2; // 2% of the span, squared (nearest match must be close)
    const out = new Set<number>();
    for (const t of tris) {
        const c = centroid(positions, indices, t);
        const target: [number, number, number] = [c[0], c[1], c[2]];
        target[axis] = 2 * plane - c[axis]; // reflect across the plane
        let best = -1;
        let bestD = Infinity;
        for (let o = 0; o < triCount; o++) {
            const oc = centroid(positions, indices, o);
            const d = (oc[0] - target[0]) ** 2 + (oc[1] - target[1]) ** 2 + (oc[2] - target[2]) ** 2;
            if (d < bestD) {
                bestD = d;
                best = o;
            }
        }
        if (best >= 0 && bestD <= limit) out.add(best);
    }
    return out;
}
