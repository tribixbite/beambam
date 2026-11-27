/**
 * Bambu / PrusaSlicer per-triangle multi-material paint (`paint_color` on a 3MF <triangle>).
 *
 * The attribute is TriangleSelector's serialisation: hex digits read from the END of the
 * string, one nibble per node, depth first. For a node nibble `d`:
 *   - `d & 3` = number of split sides (0 = leaf); a split node has `split + 1` children.
 *   - leaf: state = `d >> 2`; when that is 3 the state continues: each following 0xF nibble
 *     adds 15 and the first nibble below 0xF adds itself and ends it (filaments 18+).
 *   - split: `d >> 2` is the special side, then the children follow.
 * State 0 = not painted (the part's own filament); state k >= 1 = filament k.
 * Examples: "4" -> 1, "8" -> 2, "0C" -> 3, "1C" -> 4.
 *
 * The preview only needs one colour per triangle: the whole-triangle state when the root is
 * a leaf, otherwise the state covering most of its area. Ported from pokefit/paint.py (MIT).
 */

const HEX = '0123456789ABCDEF';

/**
 * The nibbles of a whole-triangle leaf painted with `state` (0 = unpainted). Mirror of
 * pokefit's `_leaf_nibbles`: states 0-2 are one nibble `state << 2`; state 3+ is the `0b1100`
 * marker then `0xF` per 15 and a final remainder nibble (so filaments 18+ round-trip).
 */
function leafNibbles(state: number): number[] {
    if (state < 3) return [state << 2];
    let n = state - 3;
    const out = [0b1100];
    while (n >= 15) {
        out.push(0xf);
        n -= 15;
    }
    out.push(n);
    return out;
}

/**
 * The `paint_color` string that paints a whole triangle with filament `state` (1-based; the
 * decoder's `rootState`/`majorityState` read it back). `state <= 0` is unpainted and returns
 * '' — the caller omits the attribute entirely. This is the inverse of `rootState` for leaves;
 * split (partially painted) triangles are authored elsewhere.
 */
export function encodePaint(state: number): string {
    if (state <= 0) return '';
    const nib = leafNibbles(state);
    let s = '';
    for (let i = nib.length - 1; i >= 0; i--) s += HEX[nib[i]];
    return s;
}

/** Nibbles in reading order (last character first). */
function nibbles(code: string): number[] {
    const out: number[] = [];
    for (let i = code.length - 1; i >= 0; i--) {
        const v = parseInt(code[i], 16);
        if (!Number.isNaN(v)) out.push(v);
    }
    return out;
}

/** Read a leaf state starting at `pos` (the leaf nibble itself); returns [state, next position]. */
function readLeaf(nib: number[], pos: number): [number, number] {
    let state = nib[pos] >> 2;
    pos += 1;
    if (state === 3) {
        while (pos < nib.length && nib[pos] === 0xf) {
            state += 15;
            pos += 1;
        }
        state += pos < nib.length ? nib[pos] : 0;
        pos += 1;
    }
    return [state, pos];
}

/**
 * The state of a triangle painted as a whole (its root is a leaf), or null when the
 * triangle is split into differently painted pieces. An empty string reads as 0.
 */
export function rootState(code: string): number | null {
    const nib = nibbles(code.trim());
    if (nib.length === 0) return 0;
    if ((nib[0] & 3) !== 0) return null;
    return readLeaf(nib, 0)[0];
}

/** Area share of every state in a painted triangle (each child of a split takes an equal share). */
export function paintWeights(code: string): Map<number, number> {
    const nib = nibbles(code.trim());
    const weights = new Map<number, number>();
    let pos = 0;
    const node = (share: number): void => {
        if (pos >= nib.length) return;
        const split = nib[pos] & 3;
        if (split === 0) {
            const [state, next] = readLeaf(nib, pos);
            pos = next;
            weights.set(state, (weights.get(state) ?? 0) + share);
            return;
        }
        pos += 1;
        const n = split + 1;
        for (let i = 0; i < n; i++) node(share / n);
    };
    node(1);
    return weights;
}

/** The state covering most of a painted triangle's area (0 = mostly unpainted). */
export function majorityState(code: string): number {
    const whole = rootState(code);
    if (whole !== null) return whole;
    let best = 0;
    let bestShare = -1;
    for (const [state, share] of paintWeights(code)) {
        if (share > bestShare) {
            best = state;
            bestShare = share;
        }
    }
    return best;
}

/**
 * A memoised `majorityState`: painted models repeat a handful of strings across hundreds of
 * thousands of triangles (a 1.9M-triangle Jolteon has 3), so decode each distinct one once.
 */
export function paintDecoder(): (code: string) => number {
    const cache = new Map<string, number>();
    return (code: string): number => {
        let v = cache.get(code);
        if (v === undefined) {
            v = majorityState(code);
            if (cache.size < 65536) cache.set(code, v);
        }
        return v;
    };
}
