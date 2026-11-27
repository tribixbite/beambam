import { describe, expect, test } from 'bun:test';
import { parseGcode } from './gcodepath';

const GCODE = `; a Bambu-style plate
; total layer number: 2
M83
; CHANGE_LAYER
; Z_HEIGHT: 0.2
G1 X0 Y0 Z0.2
G1 X10 Y0 E0.5
G1 X10 Y10 E0.5
G1 X0 Y10 F3000
; CHANGE_LAYER
; Z_HEIGHT: 0.4
G1 X0 Y0 Z0.4
G1 X10 Y0 E0.5
`;

describe('parseGcode', () => {
    test('splits extrusion moves into layers', () => {
        const g = parseGcode(GCODE);
        expect(g.layers).toBe(2);
        expect(g.layerZ).toEqual([0.2, 0.4]);
        // 3 extrusion segments total (2 in layer 0, 1 in layer 1) = 6 vertices = 18 floats.
        expect(g.positions.length).toBe(18);
        expect(g.vertexAtLayer).toEqual([0, 4, 6]);
    });

    test('a travel move (no E) is not a segment', () => {
        // "G1 X0 Y10 F3000" has no E → skipped; layer 0 keeps its 2 extrusion segments.
        const g = parseGcode(GCODE);
        expect(g.vertexAtLayer[1] - g.vertexAtLayer[0]).toBe(4); // 2 segments in layer 0
    });

    test('bounds cover the extruded geometry', () => {
        const g = parseGcode(GCODE);
        expect(g.min[0]).toBe(0);
        expect(g.max[0]).toBe(10);
        expect(g.max[1]).toBe(10);
        expect(g.min[2]).toBeCloseTo(0.2);
        expect(g.max[2]).toBeCloseTo(0.4);
    });

    test('no layers for non-layered gcode', () => {
        const g = parseGcode('G1 X1 Y1\nG1 X2 Y2 E1\n');
        expect(g.layers).toBe(0);
        expect(g.positions.length).toBe(0); // extrusion before any layer marker is skipped
    });
});
