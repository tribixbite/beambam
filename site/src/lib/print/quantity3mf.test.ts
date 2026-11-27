import { describe, expect, test } from 'bun:test';
import { strToU8, zipSync } from 'fflate';

import { loadModel } from './load';
import { hasQuantityChange, rewriteBuild, writeQuantity3mf, type QuantitySpec } from './quantity3mf';

const MODEL = `<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources>
  <object id="1" type="model">
   <mesh>
    <vertices>
     <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
    </vertices>
    <triangles><triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="2" v3="3"/></triangles>
   </mesh>
  </object>
 </resources>
 <build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 50 50 0" printable="1"/></build>
</model>`;

function make3mf(model = MODEL): Blob {
    return new Blob([zipSync({ '3D/3dmodel.model': strToU8(model) }) as BlobPart]);
}

describe('rewriteBuild', () => {
    test('duplicates the item for each copy, offsetting the translation', () => {
        const spec: QuantitySpec = new Map([['1', [{ dx: 0, dy: 0 }, { dx: 30, dy: 0 }, { dx: 0, dy: 30 }]]]);
        const out = rewriteBuild(MODEL, spec);
        const items = out.match(/<item\b[^>]*\/>/g) ?? [];
        expect(items).toHaveLength(3);
        // the translations: 50,50 then 80,50 then 50,80
        expect(items[0]).toContain('transform="1 0 0 0 1 0 0 0 1 50 50 0"');
        expect(items[1]).toContain('80 50 0');
        expect(items[2]).toContain('50 80 0');
    });

    test('count 0 drops the object', () => {
        const out = rewriteBuild(MODEL, new Map([['1', []]]));
        expect(out.match(/<item\b[^>]*\/>/g)).toBeNull();
    });

    test('an object not in the spec is left alone', () => {
        const out = rewriteBuild(MODEL, new Map([['9', [{ dx: 0, dy: 0 }]]]));
        expect((out.match(/<item\b[^>]*\/>/g) ?? []).length).toBe(1);
    });
});

describe('hasQuantityChange', () => {
    test('true only when a count differs from the base', () => {
        const base = new Map([['1', 1]]);
        expect(hasQuantityChange(new Map([['1', [{ dx: 0, dy: 0 }]]]), base)).toBe(false);
        expect(hasQuantityChange(new Map([['1', [{ dx: 0, dy: 0 }, { dx: 1, dy: 0 }]]]), base)).toBe(true);
        expect(hasQuantityChange(new Map([['1', []]]), base)).toBe(true);
    });
});

describe('writeQuantity3mf round-trips through loadModel', () => {
    test('three copies load as three placed parts', async () => {
        const spec: QuantitySpec = new Map([['1', [{ dx: 0, dy: 0 }, { dx: 30, dy: 0 }, { dx: 0, dy: 30 }]]]);
        const out = await writeQuantity3mf(make3mf(), spec);
        const model = await loadModel('q.3mf', out as Blob);
        expect(model.parts).toHaveLength(3);
        // the three copies sit at different XY (their translations differ by 30).
        const xs = model.parts.map((p) => p.positions[0]);
        expect(new Set(xs.map((x) => Math.round(x))).size).toBeGreaterThan(1);
    });
});
