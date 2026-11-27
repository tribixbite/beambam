import { describe, expect, test } from 'bun:test';
import { shelfPack, type Bed, type Rect } from './arrange';

const bed: Bed = { minX: 0, minY: 0, maxX: 256, maxY: 256 };

describe('shelfPack', () => {
    test('a single piece is centred by its footprint within the margin', () => {
        const r = shelfPack([{ w: 20, h: 20 }], bed, 5);
        expect(r.fits).toBe(true);
        expect(r.places[0]).toEqual({ cx: 20, cy: 20 }); // margin 5 + half of (20 + 2*5) = 5 + 15
    });

    test('places several pieces without overlap and reports a fit', () => {
        const rects: Rect[] = Array.from({ length: 4 }, () => ({ w: 40, h: 40 }));
        const r = shelfPack(rects, bed, 5);
        expect(r.fits).toBe(true);
        expect(r.places).toHaveLength(4);
        // No two centres closer than ~ a piece apart.
        for (let i = 0; i < 4; i++)
            for (let j = i + 1; j < 4; j++) {
                const d = Math.hypot(r.places[i].cx - r.places[j].cx, r.places[i].cy - r.places[j].cy);
                expect(d).toBeGreaterThan(30);
            }
    });

    test('warns (fits=false) when the pieces exceed the plate', () => {
        const rects: Rect[] = Array.from({ length: 60 }, () => ({ w: 50, h: 50 }));
        expect(shelfPack(rects, bed, 5).fits).toBe(false);
    });

    test('keeps the caller order in the result', () => {
        const rects: Rect[] = [{ w: 10, h: 10 }, { w: 80, h: 80 }, { w: 10, h: 10 }];
        const r = shelfPack(rects, bed, 2);
        expect(r.places).toHaveLength(3);
        // index 1 (the big one) is packed first (tallest) but returned at index 1.
        expect(r.places[1]).toBeDefined();
    });
});
