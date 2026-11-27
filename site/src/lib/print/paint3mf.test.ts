import { describe, expect, test } from 'bun:test';
import { strToU8, zipSync } from 'fflate';

import { loadModel } from './load';
import { hasPaint, paintModelXml, writePainted3mf, type PaintMap } from './paint3mf';

const MODEL = `<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model">
   <mesh>
    <vertices>
     <vertex x="0" y="0" z="0"/>
     <vertex x="10" y="0" z="0"/>
     <vertex x="10" y="10" z="0"/>
     <vertex x="0" y="10" z="0"/>
    </vertices>
    <triangles>
     <triangle v1="0" v2="1" v3="2"/>
     <triangle v1="0" v2="2" v3="3"/>
    </triangles>
   </mesh>
  </object>
 </resources>
 <build>
  <item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
 </build>
</model>`;

function make3mf(model = MODEL): Blob {
    const zipped = zipSync({ '3D/3dmodel.model': strToU8(model) });
    return new Blob([zipped as BlobPart]);
}

function paintOne(obj: string, tri: number, state: number): PaintMap {
    return new Map([['3D/3dmodel.model', new Map([[obj, new Map([[tri, state]])]])]]);
}

describe('paintModelXml', () => {
    test('sets paint_color on the named triangle only, by per-object index', () => {
        const out = paintModelXml(MODEL, new Map([['1', new Map([[1, 2]])]]));
        const tris = out.match(/<triangle\b[^>]*\/>/g) ?? [];
        expect(tris).toHaveLength(2);
        expect(tris[0]).not.toContain('paint_color');
        expect(tris[1]).toContain('paint_color="8"'); // state 2 -> "8"
    });

    test('replaces an existing paint_color rather than doubling it', () => {
        const painted = paintModelXml(MODEL, new Map([['1', new Map([[0, 1]])]]));
        const repainted = paintModelXml(painted, new Map([['1', new Map([[0, 3]])]]));
        const first = (repainted.match(/<triangle\b[^>]*\/>/g) ?? [])[0] ?? '';
        expect(first.match(/paint_color/g)).toHaveLength(1);
        expect(first).toContain('paint_color="0C"'); // state 3
    });

    test('state 0 clears existing paint', () => {
        const painted = paintModelXml(MODEL, new Map([['1', new Map([[0, 2]])]]));
        const cleared = paintModelXml(painted, new Map([['1', new Map([[0, 0]])]]));
        expect(cleared).not.toContain('paint_color');
    });

    test('an unknown object id paints nothing', () => {
        expect(paintModelXml(MODEL, new Map([['9', new Map([[0, 2]])]]))).not.toContain('paint_color');
    });
});

describe('hasPaint', () => {
    test('true only when some triangle has a state > 0', () => {
        expect(hasPaint(null)).toBe(false);
        expect(hasPaint(paintOne('1', 0, 0))).toBe(false);
        expect(hasPaint(paintOne('1', 0, 2))).toBe(true);
    });
});

describe('writePainted3mf round-trips through loadModel', () => {
    test('a painted triangle comes back as that filament state', async () => {
        const out = await writePainted3mf(make3mf(), paintOne('1', 1, 2));
        const model = await loadModel('painted.3mf', out as Blob);
        expect(model.parts).toHaveLength(1);
        expect(Array.from(model.parts[0].states ?? [])).toEqual([0, 2]);
    });

    test('nothing painted returns the original blob untouched', async () => {
        const blob = make3mf();
        expect(await writePainted3mf(blob, paintOne('1', 0, 0))).toBe(blob);
    });

    test('other entries survive the rewrite', async () => {
        const zipped = zipSync({
            '3D/3dmodel.model': strToU8(MODEL),
            '[Content_Types].xml': strToU8('<Types/>'),
            'Metadata/project_settings.config': strToU8('{"version":"1"}')
        });
        const out = await writePainted3mf(new Blob([zipped as BlobPart]), paintOne('1', 0, 1));
        const model = await loadModel('x.3mf', out as Blob);
        expect(Array.from(model.parts[0].states ?? [])).toEqual([1, 0]);
    });
});
