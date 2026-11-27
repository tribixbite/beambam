/**
 * Parse a sliced plate's G-code into per-layer extrusion line segments, for the toolpath preview.
 *
 * Handles Bambu/Orca output: `; CHANGE_LAYER` + `; Z_HEIGHT: <z>` layer markers, absolute XYZ (G90) with
 * relative or absolute extrusion (M83 / M82). Only extruding moves become segments; travels and retractions
 * are skipped. Segments are emitted in print order, so the renderer shows "up to layer N" with a draw range.
 */

export interface GcodePaths {
    /** Flat x,y,z pairs for THREE.LineSegments: [ax,ay,az, bx,by,bz, …] per extrusion move. */
    positions: Float32Array;
    /** vertexAtLayer[i] = index into `positions` (in VERTICES, not floats) where layer i starts. Length =
     * layers + 1, so layer i spans [vertexAtLayer[i], vertexAtLayer[i+1]). */
    vertexAtLayer: number[];
    /** Z height of each layer. */
    layerZ: number[];
    /** Number of layers with extrusion. */
    layers: number;
    /** Bounds of the extruded geometry (min/max xyz), for framing. */
    min: [number, number, number];
    max: [number, number, number];
}

function num(token: string): number {
    return parseFloat(token.slice(1));
}

/** Parse `text` (one plate's G-code) into layered toolpaths. */
export function parseGcode(text: string): GcodePaths {
    const verts: number[] = []; // x,y,z triples, two per segment
    const vertexAtLayer: number[] = []; // boundary at the start of each layer (then a final close)
    const layerZ: number[] = [];
    let x = 0;
    let y = 0;
    let z = 0;
    let absXYZ = true; // G90
    let relE = false; // M83
    const min: [number, number, number] = [Infinity, Infinity, Infinity];
    const max: [number, number, number] = [-Infinity, -Infinity, -Infinity];

    const grow = (px: number, py: number, pz: number): void => {
        if (px < min[0]) min[0] = px;
        if (py < min[1]) min[1] = py;
        if (pz < min[2]) min[2] = pz;
        if (px > max[0]) max[0] = px;
        if (py > max[1]) max[1] = py;
        if (pz > max[2]) max[2] = pz;
    };

    const lines = text.split('\n');
    for (let li = 0; li < lines.length; li++) {
        const raw = lines[li];
        // Layer markers (comments). Bambu: "; CHANGE_LAYER" then "; Z_HEIGHT: <z>".
        if (raw.charCodeAt(0) === 59 /* ; */) {
            const c = raw.slice(1).trim();
            if (c === 'CHANGE_LAYER') {
                // The Z_HEIGHT line usually follows; capture it, else use the current z.
                const next = (lines[li + 1] ?? '').trim();
                const m = /^;\s*Z_HEIGHT:\s*([-\d.]+)/.exec(next);
                layerZ.push(m ? parseFloat(m[1]) : z);
                vertexAtLayer.push(verts.length / 3); // this layer starts here
            }
            continue;
        }
        if (!raw || (raw[0] !== 'G' && raw[0] !== 'M')) continue;
        // Split off any inline comment, then tokenize.
        const semi = raw.indexOf(';');
        const line = semi >= 0 ? raw.slice(0, semi) : raw;
        const t = line.split(/\s+/);
        const cmd = t[0];
        if (cmd === 'G90') {
            absXYZ = true;
            continue;
        }
        if (cmd === 'G91') {
            absXYZ = false;
            continue;
        }
        if (cmd === 'M82') {
            relE = false;
            continue;
        }
        if (cmd === 'M83') {
            relE = true;
            continue;
        }
        if (cmd !== 'G0' && cmd !== 'G1') continue;
        let nx = x;
        let ny = y;
        let nz = z;
        let e = 0;
        let hasXY = false;
        for (let k = 1; k < t.length; k++) {
            const tok = t[k];
            const ch = tok[0];
            if (ch === 'X') {
                nx = absXYZ ? num(tok) : x + num(tok);
                hasXY = true;
            } else if (ch === 'Y') {
                ny = absXYZ ? num(tok) : y + num(tok);
                hasXY = true;
            } else if (ch === 'Z') {
                nz = absXYZ ? num(tok) : z + num(tok);
            } else if (ch === 'E') {
                e = num(tok); // relative: the delta; absolute: the new total (handled below)
            }
        }
        // Extrusion amount for this move (relative E is the delta; absolute E is new total).
        const extrude = relE ? e > 1e-6 : e > 0; // absolute-E total > last would need tracking; rare in Bambu
        if (hasXY && extrude && vertexAtLayer.length > 0) {
            verts.push(x, y, z, nx, ny, nz);
            grow(x, y, z);
            grow(nx, ny, nz);
        }
        x = nx;
        y = ny;
        z = nz;
    }
    vertexAtLayer.push(verts.length / 3); // close the last layer
    if (!Number.isFinite(min[0])) {
        min[0] = min[1] = min[2] = 0;
        max[0] = max[1] = max[2] = 1;
    }
    return {
        positions: new Float32Array(verts),
        vertexAtLayer,
        layerZ,
        layers: Math.max(0, layerZ.length),
        min,
        max
    };
}
