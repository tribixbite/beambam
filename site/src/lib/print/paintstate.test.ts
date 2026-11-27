import { describe, expect, test } from 'bun:test';
import type { PreviewPart } from './model';
import { PaintSession, paintable, partKey } from './paintstate';

/** A flat quad (2 coplanar triangles) as a paintable 3MF part. */
function quadPart(over: Partial<PreviewPart> = {}): PreviewPart {
    return {
        name: 'q',
        filament: 1,
        positions: new Float32Array([0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0]),
        indices: new Uint32Array([0, 1, 2, 0, 2, 3]),
        states: null,
        path: '3D/3dmodel.model',
        objectId: '1',
        ...over
    };
}

/** A unit cube as a paintable part (8 shared vertices, 12 triangles). */
function cubePart(): PreviewPart {
    return quadPart({
        positions: new Float32Array([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]),
        indices: new Uint32Array([
            0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 2, 3, 7, 2, 7, 6, 1, 2, 6, 1, 6, 5, 0, 4, 7, 0, 7, 3
        ])
    });
}
const count2 = (p: PreviewPart): number => [...(p.states ?? [])].filter((x) => x === 2).length;

describe('paintable / partKey', () => {
    test('needs a 3MF object with geometry', () => {
        expect(paintable(quadPart())).toBe(true);
        expect(paintable(quadPart({ path: '' }))).toBe(false);
        expect(paintable(quadPart({ indices: null }))).toBe(false);
    });
    test('key joins path and object id', () => {
        expect(partKey(quadPart())).toBe('3D/3dmodel.model\u00001');
    });
});

describe('PaintSession', () => {
    test('paints a region, shows it live, and reports it in the paint map', () => {
        const part = quadPart();
        const s = new PaintSession([part]);
        expect(s.paint(part, 0, 'region', 2)).toBe(true); // flat quad: both triangles
        expect(Array.from(part.states ?? [])).toEqual([2, 2]);
        expect(s.dirty).toBe(true);
        expect(s.usedFilaments()).toEqual(new Set([2]));
        const map = s.toPaintMap();
        expect(map.get('3D/3dmodel.model')?.get('1')).toEqual(new Map([[0, 2], [1, 2]]));
    });

    test('face mode paints just the tapped triangle', () => {
        const part = quadPart();
        const s = new PaintSession([part]);
        s.paint(part, 1, 'face', 3);
        expect(Array.from(part.states ?? [])).toEqual([0, 3]);
    });

    test('undo and redo walk the history', () => {
        const part = quadPart();
        const s = new PaintSession([part]);
        s.paint(part, 0, 'face', 2);
        s.paint(part, 1, 'face', 3);
        expect(Array.from(part.states ?? [])).toEqual([2, 3]);
        expect(s.undo()).toBe(true);
        expect(Array.from(part.states ?? [])).toEqual([2, 0]);
        expect(s.undo()).toBe(true);
        expect(Array.from(part.states ?? [])).toEqual([0, 0]);
        expect(s.dirty).toBe(false);
        expect(s.redo()).toBe(true);
        expect(Array.from(part.states ?? [])).toEqual([2, 0]);
    });

    test('clear restores the original state as one undoable step', () => {
        const part = quadPart({ states: new Uint8Array([1, 0]) }); // file already paints triangle 0
        const s = new PaintSession([part]);
        s.paint(part, 0, 'face', 2);
        s.paint(part, 1, 'face', 2);
        expect(Array.from(part.states ?? [])).toEqual([2, 2]);
        expect(s.clear()).toBe(true);
        expect(Array.from(part.states ?? [])).toEqual([1, 0]); // back to the file's own paint
        expect(s.toPaintMap().size).toBe(0);
        s.undo(); // clear is undoable
        expect(Array.from(part.states ?? [])).toEqual([2, 2]);
    });

    test('an edit that returns a triangle to its painted value is a no-op', () => {
        const part = quadPart();
        const s = new PaintSession([part]);
        s.paint(part, 0, 'face', 2);
        expect(s.paint(part, 0, 'face', 2)).toBe(false); // already 2
    });

    test('all instances of an object share one live buffer', () => {
        const a = quadPart();
        const b = quadPart(); // same path+objectId = same object, another placement
        const s = new PaintSession([a, b]);
        s.paint(a, 0, 'face', 4);
        expect(a.states).toBe(b.states as Uint8Array); // same buffer
        expect(Array.from(b.states ?? [])).toEqual([4, 0]);
    });

    test('grow extends the last painted area, shrink contracts it (undoable)', () => {
        const part = cubePart();
        const s = new PaintSession([part]);
        s.paint(part, 0, 'face', 2);
        const a = count2(part);
        expect(s.grow()).toBe(true);
        const b = count2(part);
        expect(b).toBeGreaterThan(a);
        expect(s.shrink()).toBe(true);
        expect(count2(part)).toBeLessThan(b);
        s.undo(); // shrink is undoable
        expect(count2(part)).toBe(b);
    });

    test('grow does nothing with no prior paint', () => {
        const s = new PaintSession([cubePart()]);
        expect(s.grow()).toBe(false);
    });

    test('clear resets the grow/shrink region', () => {
        const part = cubePart();
        const s = new PaintSession([part]);
        s.paint(part, 0, 'face', 2);
        s.clear();
        expect(s.grow()).toBe(false); // nothing to grow after clear
    });

    test('mirror paints the symmetric triangle too', () => {
        const part = cubePart();
        const s = new PaintSession([part]);
        s.paint(part, 10, 'face', 2, 30, true); // a left-face triangle, mirrored
        expect(count2(part)).toBe(2); // the tapped one and its mirror
    });

    test('stateAt reports a triangle current colour, and fill buckets the same colour', () => {
        const part = quadPart();
        const s = new PaintSession([part]);
        expect(s.stateAt(part, 0)).toBe(0);
        s.paint(part, 0, 'fill', 4); // both coplanar, same colour, connected → both painted
        expect(Array.from(part.states ?? [])).toEqual([4, 4]);
        expect(s.stateAt(part, 1)).toBe(4);
    });

    test('toPaintMap keeps only touched triangles (file paint elsewhere is preserved)', () => {
        const part = quadPart({ states: new Uint8Array([5, 5]) });
        const s = new PaintSession([part]);
        s.paint(part, 1, 'face', 2); // only triangle 1
        const obj = s.toPaintMap().get('3D/3dmodel.model')?.get('1');
        expect(obj).toEqual(new Map([[1, 2]])); // triangle 0's existing paint is left alone
    });
});
