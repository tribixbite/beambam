import { describe, expect, test } from 'bun:test';
import { strToU8, zipSync } from 'fflate';
import { loadModel, read3mf } from './load';
import { attr, bedFromArea, computeNormals, compose, determinant, ModelScanner, parseModelSettings, parseObj, parseSliceInfo, parseStl,
    parseTransform, vertexColours } from './model';

const ROOT = `<?xml version="1.0"?><model xmlns:p="x"><resources>
 <object id="2" type="model"><components>
  <component p:path="/3D/Objects/object_2.model" objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>
  <component p:path="/3D/Objects/object_2.model" objectid="3" transform="1 0 0 0 1 0 0 0 1 0 0 5"/>
 </components></object></resources>
 <build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 100 0" printable="1"/></build></model>`;
const MESH = (id: string, paint: string) => `<object id="${id}" type="model"><mesh><vertices>
 <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="0"/><vertex x="0" y="0" z="10"/>
 </vertices><triangles><triangle v1="0" v2="1" v3="2"${paint}/><triangle v1="0" v2="1" v3="3"/></triangles></mesh></object>`;
const OBJECTS = `<?xml version="1.0"?><model><resources>${MESH('1', ' paint_color="8"')}${MESH('3', '')}</resources></model>`;
const SETTINGS = `<config><object id="2"><metadata key="name" value="fig"/><metadata key="extruder" value="1"/>
 <part id="1" subtype="normal_part"><metadata key="name" value="body"/><metadata key="extruder" value="0"/></part>
 <part id="3" subtype="normal_part"><metadata key="name" value="hat"/><metadata key="extruder" value="3"/></part>
 </object></config>`;
const PROJECT = JSON.stringify({ filament_colour: ['#EEBF31', '#FFFFFF', '#562C2C'], filament_type: ['PLA', 'PLA', 'PETG'],
    printable_area: ['0x0', '350x0', '350x320', '0x320'] });

function project3mf(extra: Record<string, Uint8Array> = {}): Blob {
    return new Blob([zipSync({
        '3D/3dmodel.model': strToU8(ROOT),
        '3D/Objects/object_2.model': strToU8(OBJECTS),
        'Metadata/model_settings.config': strToU8(SETTINGS),
        'Metadata/project_settings.config': strToU8(PROJECT),
        'Metadata/plate_1.png': new Uint8Array(100),
        ...extra
    })]);
}

describe('xml helpers', () => {
    test('attr matches whole names only', () => {
        expect(attr(' max="1" x="2"', 'x')).toBe('2');
        expect(attr(' name="a &amp; b"', 'name')).toBe('a & b');
        expect(attr(' y="1"', 'x')).toBeNull();
    });
    test('transforms compose child then parent; mirror detection', () => {
        const child = parseTransform('1 0 0 0 1 0 0 0 1 1 2 3');
        const parent = parseTransform('0 1 0 -1 0 0 0 0 1 10 0 0'); // rotate 90° about Z, then +10 X
        const m = compose(child, parent);
        // point (0,0,0) -> child (1,2,3) -> parent: x' = 1*0 + 2*-1 + 10 = 8, y' = 1*1 + 2*0 = 1
        expect(m.slice(9)).toEqual([8, 1, 3]);
        expect(determinant(parseTransform('-1 0 0 0 1 0 0 0 1 0 0 0'))).toBe(-1);
        expect(parseTransform('bad')).toEqual([1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]);
    });
    test('bed from printable_area, default otherwise', () => {
        expect(bedFromArea(['0x0', '350x0', '350x320', '0x320'])).toEqual({ minX: 0, minY: 0, maxX: 350, maxY: 320 });
        expect(bedFromArea(undefined)).toEqual({ minX: 0, minY: 0, maxX: 256, maxY: 256 });
    });
});

describe('ModelScanner', () => {
    test('chunk boundaries anywhere give the same mesh', () => {
        for (const step of [1, 7, 50, 100000]) {
            const s = new ModelScanner('3D/Objects/object_2.model');
            for (let i = 0; i < OBJECTS.length; i += step) s.push(OBJECTS.slice(i, i + step));
            const f = s.end();
            const o = f.objects.get('1')!;
            expect([...o.mesh!.positions]).toEqual([0, 0, 0, 10, 0, 0, 0, 10, 0, 0, 0, 10]);
            expect([...o.mesh!.indices]).toEqual([0, 1, 2, 0, 1, 3]);
            expect([...o.mesh!.states!]).toEqual([2, 0]);
            expect(f.objects.get('3')!.mesh!.states).toBeNull();
        }
    });
});

describe('ModelScanner attribute orders', () => {
    test('other attribute orders and extra paint layers read the same', () => {
        const s = new ModelScanner('m');
        s.push(`<model><resources><object id="1"><mesh><vertices><vertex z="3" x="1" y="2"/><vertex x="4" y="5" z="6"/>
          <vertex x="7" y="8" z="9" /></vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_supports="4" paint_color="1C"/>
          <triangle v1="2" v2="1" v3="0" paint_color="8" paint_seam="4"/>
          <triangle v3="0" v1="1" v2="2" paint_color="0C"/></triangles></mesh></object></resources></model>`);
        const o = s.end().objects.get('1')!;
        expect([...o.mesh!.positions]).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
        expect([...o.mesh!.indices]).toEqual([0, 1, 2, 2, 1, 0, 1, 2, 0]);
        expect([...o.mesh!.states!]).toEqual([4, 2, 3]);
    });
});

describe('normals', () => {
    test('a flat triangle facing +Z, indexed or not', () => {
        const pos = new Float32Array([0, 0, 0, 1, 0, 0, 0, 1, 0]);
        expect([...computeNormals(pos, null)]).toEqual([0, 0, 1, 0, 0, 1, 0, 0, 1]);
        expect([...computeNormals(pos, new Uint32Array([0, 1, 2])).slice(0, 3)]).toEqual([0, 0, 1]);
    });
});

describe('metadata', () => {
    test('model settings: object and part extruders', () => {
        const s = parseModelSettings(SETTINGS).get('2')!;
        expect(s.extruder).toBe(1);
        expect(s.parts.get('3')).toEqual({ extruder: 3, subtype: 'normal_part', name: 'hat' });
    });
    test('slice info', () => {
        const r = parseSliceInfo(`<config><plate><metadata key="prediction" value="9864"/><metadata key="weight" value="25.88"/>
          <filament id="1" tray_info_idx="GFG99" type="PETG" color="#EEBF31" used_g="15.96"/>
          <filament id="4" type="PLA" color="#000000" used_g="1.84"/></plate></config>`)!;
        expect(r.info).toEqual({ seconds: 9864, grams: 25.88, used: [1, 4] });
        expect(r.filaments.get(1)).toEqual({ colour: '#EEBF31', type: 'PETG', grams: 15.96, profileId: 'GFG99' });
    });
});

describe('loadModel', () => {
    test('a Bambu project: parts placed, filaments, paint, bed', async () => {
        const m = await loadModel('fig.3mf', project3mf());
        expect(m.kind).toBe('project');
        expect(m.filaments.map((f) => `${f.type} ${f.colour}`)).toEqual(['PLA #EEBF31', 'PLA #FFFFFF', 'PETG #562C2C']);
        expect(m.bed.maxX).toBe(350);
        expect(m.parts.map((p) => [p.name, p.filament])).toEqual([['body', 1], ['hat', 3]]);
        expect([...m.parts[0].positions.slice(0, 3)]).toEqual([100, 100, 0]);
        expect([...m.parts[1].positions.slice(0, 3)]).toEqual([100, 100, 5]);
        expect(m.triangles).toBe(4);
        // Painted triangle 0 -> filament 2 (#FFFFFF) on its vertices 0..2; vertex 3 keeps filament 1.
        const c = vertexColours(m.parts[0], m.filaments.map((f) => f.colour));
        expect([...c.slice(0, 3)]).toEqual([255, 255, 255]);
        expect([...c.slice(9, 12)]).toEqual([0xee, 0xbf, 0x31]);
    });
    test('the fflate fallback reads the same project', async () => {
        const m = await read3mf('fig.3mf', project3mf());
        expect(m.parts.map((p) => [p.name, p.filament])).toEqual([['body', 1], ['hat', 3]]);
        expect([...m.parts[0].states!]).toEqual([2, 0]);
    });
    test('a geometry-only 3MF is one filament and lands on the bed', async () => {
        const blob = new Blob([zipSync({ '3D/3dmodel.model': strToU8(`<model><resources>${MESH('5', '')}</resources>
          <build><item objectid="5" transform="1 0 0 0 1 0 0 0 1 -500 -500 -3"/></build></model>`) })]);
        const m = await loadModel('geo.3mf', blob);
        expect(m.kind).toBe('geometry');
        expect(m.filaments).toHaveLength(1);
        expect([...m.parts[0].positions.slice(0, 3)]).toEqual([123, 123, 0]);
    });
    test('a sliced file takes its filaments from slice_info', async () => {
        const m = await loadModel('fig.gcode.3mf', project3mf({ 'Metadata/slice_info.config': strToU8(
            '<config><plate><metadata key="prediction" value="60"/><metadata key="weight" value="2"/><filament id="2" type="PETG" color="#00FF00" used_g="2"/></plate></config>') }));
        expect(m.kind).toBe('sliced');
        expect(m.sliced).toEqual({ seconds: 60, grams: 2, used: [2] });
        expect(m.filaments[1]).toMatchObject({ type: 'PETG', colour: '#00FF00', grams: 2 });
    });
    test('binary + ASCII STL, OBJ', async () => {
        const buf = new ArrayBuffer(84 + 50);
        const dv = new DataView(buf);
        dv.setUint32(80, 1, true);
        [0, 0, 0, 20, 0, 0, 0, 20, 0].forEach((v, i) => dv.setFloat32(84 + 12 + i * 4, v, true));
        expect(parseStl(new Uint8Array(buf))).toHaveLength(9);
        const ascii = 'solid a\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid';
        expect([...parseStl(strToU8(ascii))]).toEqual([0, 0, 0, 1, 0, 0, 0, 1, 0]);
        const obj = parseObj('v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1/1 2/2 3/3 4/4\nf -1 -2 -3\n');
        expect([...obj.indices]).toEqual([0, 1, 2, 0, 2, 3, 3, 2, 1]);
        const m = await loadModel('a.stl', new Blob([buf]));
        expect(m.kind).toBe('mesh');
        expect([...m.parts[0].positions.slice(0, 3)]).toEqual([118, 118, 0]);
    });
    test('unsupported files say what is supported', async () => {
        await expect(loadModel('a.step', new Blob(['x']))).rejects.toThrow('.3mf, .gcode.3mf, .stl and .obj');
    });
});
