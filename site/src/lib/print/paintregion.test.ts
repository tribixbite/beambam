import { describe, expect, test } from 'bun:test';
import { regionAround, triangleNormal, type RegionMesh } from './paintregion';

/** A unit cube centred at the origin: 8 shared vertices, 12 triangles (2 per face). */
function cube(): RegionMesh {
    const positions = new Float32Array([
        0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, // 0..3 bottom z=0
        0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1 //  4..7 top z=1
    ]);
    // Each face as two triangles, consistent outward winding is not needed for these tests.
    const indices = new Uint32Array([
        0, 2, 1, 0, 3, 2, // bottom (z=0)
        4, 5, 6, 4, 6, 7, // top (z=1)
        0, 1, 5, 0, 5, 4, // front (y=0)
        2, 3, 7, 2, 7, 6, // back (y=1)
        1, 2, 6, 1, 6, 5, // right (x=1)
        0, 4, 7, 0, 7, 3 //  left (x=0)
    ]);
    return { positions, indices };
}

/** A flat quad (two coplanar triangles). */
function quad(): RegionMesh {
    return {
        positions: new Float32Array([0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0]),
        indices: new Uint32Array([0, 1, 2, 0, 2, 3])
    };
}

describe('regionAround', () => {
    test('face mode selects only the tapped triangle', () => {
        expect([...regionAround(cube(), 5, 'face')]).toEqual([5]);
    });

    test('part mode selects the whole connected shell', () => {
        expect(regionAround(cube(), 0, 'part').size).toBe(12);
    });

    test('region mode stops at the cube edges (90° creases) — one face', () => {
        // Bottom face = triangles 0 and 1; its neighbours across a cube edge bend 90°.
        const r = regionAround(cube(), 0, 'region', 30);
        expect([...r].sort((a, b) => a - b)).toEqual([0, 1]);
    });

    test('region mode floods a flat surface (no crease)', () => {
        expect(regionAround(quad(), 0, 'region', 30).size).toBe(2);
    });

    test('a large crease angle floods across the cube edges', () => {
        expect(regionAround(cube(), 0, 'region', 170).size).toBe(12);
    });

    test('an out-of-range seed selects nothing', () => {
        expect(regionAround(cube(), 99, 'part').size).toBe(0);
    });

    test('fill selects the connected same-colour area', () => {
        const c = cube();
        const states = new Uint8Array(12); // all one colour
        expect(regionAround(c, 0, 'fill', 30, undefined, states).size).toBe(12);
        states[0] = 2;
        states[1] = 2; // the bottom face is another colour now
        const r = regionAround(c, 2, 'fill', 30, undefined, states); // fill from a different-colour triangle
        expect(r.has(0)).toBe(false);
        expect(r.has(1)).toBe(false);
        expect(r.size).toBe(10);
    });
});

describe('triangleNormal', () => {
    test('unit length, points along +z for a ccw z=0 triangle', () => {
        const n = triangleNormal(new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]), new Uint32Array([0, 1, 2]), 0);
        expect(n[2]).toBeCloseTo(1);
        expect(Math.hypot(...n)).toBeCloseTo(1);
    });
});
