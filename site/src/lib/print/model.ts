/**
 * Model files -> preview parts (plate coordinates, mm, Z up) and the file's filaments.
 *
 * 3MF (Bambu Studio / Orca projects, geometry-only exports, sliced .gcode.3mf):
 *   3D/3dmodel.model           build items (object id + transform) and objects whose
 *                              <components> point at meshes (p:path + objectid + transform)
 *   3D/Objects/object_N.model  the meshes; triangles may carry `paint_color` (MMU painting)
 *   Metadata/model_settings.config   per object / part: extruder (1-based filament), subtype
 *   Metadata/project_settings.config JSON: filament_colour, filament_type, printable_area
 *   Metadata/slice_info.config       (sliced files) per-filament grams, time, weight
 *
 * Model XML can be hundreds of MB (a painted 1.9M-triangle figure is 172 MB), so meshes are
 * read by `ModelScanner`, a streaming tag scanner fed decoded text chunk by chunk; nothing
 * builds a DOM (DOMParser does not exist in a Web Worker anyway).
 */
import { normHex } from './colour';
import { paintDecoder } from './paint';

/** Row-vector 3MF affine transform: m00 m01 m02 m10 m11 m12 m20 m21 m22 m30 m31 m32. */
export type Xf = number[];
export const IDENTITY: Xf = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];

/** A mesh as read: vertices, triangle indices, and the paint state of each triangle (0 = unpainted). */
export interface RawMesh {
    positions: Float32Array;
    indices: Uint32Array;
    /** Per triangle; null when nothing is painted. */
    states: Uint8Array | null;
}

/** One preview part: a mesh in plate coordinates and the filament it prints with. */
export interface PreviewPart {
    name: string;
    /** 1-based filament number. */
    filament: number;
    positions: Float32Array;
    /** null: non-indexed (every three vertices are a triangle, e.g. STL). */
    indices: Uint32Array | null;
    states: Uint8Array | null;
    /**
     * Source of this part's mesh, for writing paint back (paint3mf.ts): the model entry path and
     * the `<object>` id. Empty for an STL/OBJ mesh (no 3MF entry to paint). Several instances of
     * one object share these, so painting one paints them all (paint lives on the object mesh).
     */
    path?: string;
    objectId?: string;
    /** The root `<build><item>` object id this part descended from (what the quantity feature duplicates;
     * differs from objectId when the item is an assembly that references the mesh via components). */
    buildObjectId?: string;
    /** Per-vertex normals (filled by `withNormals`, in the worker). */
    normals?: Float32Array;
    /** The project plate (1-based) the part is on; positions are as stored, plate N offset from plate 1. */
    plate?: number;
}

/** A filament of the file (index 0 = filament 1). */
export interface FileFilament {
    colour: string;
    type: string;
    /** Sliced files: grams used, else null. */
    grams: number | null;
    /** Bambu filament preset id when the file names one. */
    profileId: string;
}

/** What a sliced .gcode.3mf says about itself. */
export interface SlicedInfo {
    seconds: number;
    grams: number;
    /** Filament numbers (1-based) the plate uses. */
    used: number[];
}

export type ModelKind = 'project' | 'geometry' | 'sliced' | 'mesh';

/** Everything the page needs from a dropped file. */
export interface LoadedModel {
    name: string;
    /** project: a Bambu/Orca project; geometry: a 3MF with no print settings; mesh: STL/OBJ. */
    kind: ModelKind;
    parts: PreviewPart[];
    filaments: FileFilament[];
    bed: { minX: number; minY: number; maxX: number; maxY: number };
    triangles: number;
    sliced: SlicedInfo | null;
    /** The build plate the project is set up for (`curr_bed_type`), null when it names none. */
    plate: string | null;
    /** How many plates the project has (1 for anything that is not a multi-plate project). */
    plates: number;
}

export const DEFAULT_BED = { minX: 0, minY: 0, maxX: 256, maxY: 256 };
/** The colour a mesh shows before a spool is chosen. */
export const NEUTRAL = '#C8C8C8';
/** Preview budget: placed parts and triangles a 3MF may expand to (a phone has ~2M triangles in hand). */
export const MAX_PARTS = 5000;
export const MAX_TRIANGLES = 12_000_000;

// ------------------------------------------------------------------ XML attribute helpers

/** The value of attribute `name` in a tag's attribute text, or null. XML entities are decoded. */
export function attr(text: string, name: string): string | null {
    let from = 0;
    for (;;) {
        const i = text.indexOf(`${name}="`, from);
        if (i < 0) return null;
        // Must start a name: `x="` must not match the tail of `max="`.
        const prev = i > 0 ? text.charCodeAt(i - 1) : 32;
        if (prev === 32 || prev === 9 || prev === 10 || prev === 13) {
            const start = i + name.length + 2;
            const end = text.indexOf('"', start);
            if (end < 0) return null;
            const v = text.slice(start, end);
            return v.includes('&') ? decodeEntities(v) : v;
        }
        from = i + 1;
    }
}

function decodeEntities(s: string): string {
    return s
        .replace(/&quot;/g, '"')
        .replace(/&apos;/g, "'")
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&');
}

/** A 3MF transform attribute (12 numbers), identity when absent or malformed. */
export function parseTransform(s: string | null | undefined): Xf {
    if (!s) return [...IDENTITY];
    const v = s.trim().split(/\s+/).map(Number);
    return v.length === 12 && v.every(Number.isFinite) ? v : [...IDENTITY];
}

/** `a` then `b` (row vectors: p * a * b). */
export function compose(a: Xf, b: Xf): Xf {
    const out = new Array<number>(12);
    for (let r = 0; r < 4; r++) {
        for (let c = 0; c < 3; c++) {
            let s = r === 3 ? b[9 + c] : 0;
            for (let k = 0; k < 3; k++) s += a[r * 3 + k] * b[k * 3 + c];
            out[r * 3 + c] = s;
        }
    }
    return out;
}

/** Determinant of the linear part (negative = mirrored). */
export function determinant(m: Xf): number {
    return (
        m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6]) + m[2] * (m[3] * m[7] - m[4] * m[6])
    );
}

/** Positions transformed by `m` into a new array. */
export function transformPositions(src: Float32Array, m: Xf): Float32Array {
    const out = new Float32Array(src.length);
    for (let i = 0; i < src.length; i += 3) {
        const x = src[i];
        const y = src[i + 1];
        const z = src[i + 2];
        out[i] = x * m[0] + y * m[3] + z * m[6] + m[9];
        out[i + 1] = x * m[1] + y * m[4] + z * m[7] + m[10];
        out[i + 2] = x * m[2] + y * m[5] + z * m[8] + m[11];
    }
    return out;
}

// ------------------------------------------------------------------ growable typed arrays

class Grow<T extends Float32Array | Uint32Array | Uint8Array> {
    buf: T;
    len = 0;
    constructor(private readonly make: (n: number) => T, initial = 4096) {
        this.buf = make(initial);
    }
    push(v: number): void {
        if (this.len === this.buf.length) {
            const next = this.make(this.buf.length * 2);
            next.set(this.buf);
            this.buf = next;
        }
        this.buf[this.len++] = v;
    }
    /** The filled part, trimmed (a copy only when there is slack). */
    done(): T {
        return (this.len === this.buf.length ? this.buf : this.buf.slice(0, this.len)) as T;
    }
}

// ------------------------------------------------------------------ streaming model scanner

/** A <component> reference. */
export interface ComponentRef {
    /** Model file path inside the zip, without a leading slash. */
    path: string;
    objectId: string;
    transform: Xf;
}

/** One <object> of a model file. */
export interface ModelObject {
    id: string;
    type: string;
    mesh: RawMesh | null;
    components: ComponentRef[];
}

/** One <build><item>. */
export interface BuildItem {
    objectId: string;
    transform: Xf;
    printable: boolean;
}

/** Everything one .model file holds. */
export interface ModelFile {
    path: string;
    objects: Map<string, ModelObject>;
    build: BuildItem[];
}

/** "/3D/Objects/a.model" -> "3D/Objects/a.model". */
export function zipPath(p: string): string {
    return p.replace(/^\/+/, '');
}

/**
 * Fast paths first: Bambu, Orca and PrusaSlicer write vertices and triangles with their
 * attributes in this order, and matching the numbers directly skips a substring + attribute
 * search per tag (millions of them). Any other tag, or another attribute order, falls through
 * to the generic alternative (groups 9-11).
 */
const TAG =
    /<vertex x="([^"]*)" y="([^"]*)" z="([^"]*)"\s*\/>|<triangle v1="(\d+)" v2="(\d+)" v3="(\d+)"(?: paint_color="([0-9A-Fa-f]*)")?(\s*\/>)?|<(\/?)([A-Za-z][\w:.-]*)([^>]*)>/g;

/**
 * Reads one .model file from text chunks (any split points). Only the tags a preview needs
 * are looked at: object, vertex, triangle, component, item.
 */
export class ModelScanner {
    private carry = '';
    private readonly objects = new Map<string, ModelObject>();
    private readonly build: BuildItem[] = [];
    private current: ModelObject | null = null;
    private verts: Grow<Float32Array> | null = null;
    private tris: Grow<Uint32Array> | null = null;
    private states: Grow<Uint8Array> | null = null;
    private painted = false;
    private readonly decode = paintDecoder();

    constructor(readonly path: string) {}

    /** Feed the next chunk of the file's text. */
    push(text: string): void {
        const buf = this.carry + text;
        const last = buf.lastIndexOf('>');
        if (last < 0) {
            this.carry = buf;
            return;
        }
        this.carry = buf.slice(last + 1);
        this.scan(buf.slice(0, last + 1));
    }

    private scan(text: string): void {
        TAG.lastIndex = 0;
        let m: RegExpExecArray | null;
        while ((m = TAG.exec(text)) !== null) {
            if (m[1] !== undefined) {
                if (this.verts) {
                    this.verts.push(+m[1]);
                    this.verts.push(+m[2]);
                    this.verts.push(+m[3]);
                }
                continue;
            }
            if (m[4] !== undefined && m[8] !== undefined) {
                if (this.tris && this.states) {
                    this.tris.push(+m[4]);
                    this.tris.push(+m[5]);
                    this.tris.push(+m[6]);
                    const st = m[7] ? Math.min(255, this.decode(m[7])) : 0;
                    if (st) this.painted = true;
                    this.states.push(st);
                }
                continue;
            }
            if (m[4] !== undefined) {
                // A triangle with more attributes after v3 (another paint layer first): read it generically.
                const at = m[0].slice('<triangle'.length);
                const rest = text.indexOf('>', TAG.lastIndex - 1);
                this.triangle(at + text.slice(TAG.lastIndex, rest));
                TAG.lastIndex = rest + 1;
                continue;
            }
            const name = m[10];
            if (m[9] === '/') {
                if (name === 'object') this.closeObject();
                continue;
            }
            const a = m[11];
            switch (name) {
                case 'vertex':
                    if (this.verts) {
                        this.verts.push(Number(attr(a, 'x')));
                        this.verts.push(Number(attr(a, 'y')));
                        this.verts.push(Number(attr(a, 'z')));
                    }
                    break;
                case 'triangle':
                    this.triangle(a);
                    break;
                case 'object': {
                    this.closeObject();
                    const id = attr(a, 'id') ?? '';
                    this.current = { id, type: attr(a, 'type') ?? 'model', mesh: null, components: [] };
                    if (a.trimEnd().endsWith('/')) this.closeObject();
                    break;
                }
                case 'mesh':
                    if (this.current) {
                        this.verts = new Grow((n) => new Float32Array(n));
                        this.tris = new Grow((n) => new Uint32Array(n));
                        this.states = new Grow((n) => new Uint8Array(n));
                        this.painted = false;
                    }
                    break;
                case 'component':
                    if (this.current) {
                        this.current.components.push({
                            path: zipPath(attr(a, 'p:path') ?? this.path),
                            objectId: attr(a, 'objectid') ?? '',
                            transform: parseTransform(attr(a, 'transform'))
                        });
                    }
                    break;
                case 'item':
                    this.build.push({
                        objectId: attr(a, 'objectid') ?? '',
                        transform: parseTransform(attr(a, 'transform')),
                        printable: attr(a, 'printable') !== '0'
                    });
                    break;
            }
        }
    }

    /** A <triangle> from its attribute text (any attribute order). */
    private triangle(a: string): void {
        if (!this.tris || !this.states) return;
        this.tris.push(Number(attr(a, 'v1')));
        this.tris.push(Number(attr(a, 'v2')));
        this.tris.push(Number(attr(a, 'v3')));
        // Bambu writes paint_color; PrusaSlicer the same encoding as mmu_segmentation.
        const code = attr(a, 'paint_color') ?? attr(a, 'slic3rpe:mmu_segmentation');
        const st = code ? Math.min(255, this.decode(code)) : 0;
        if (st) this.painted = true;
        this.states.push(st);
    }

    private closeObject(): void {
        const o = this.current;
        if (!o) return;
        if (this.verts && this.tris && this.states) {
            const tri = this.tris.done();
            o.mesh = {
                positions: this.verts.done(),
                indices: tri,
                states: this.painted ? this.states.done() : null
            };
        }
        this.objects.set(o.id, o);
        this.current = null;
        this.verts = this.tris = this.states = null;
    }

    /** Finish the file. */
    end(): ModelFile {
        if (this.carry.includes('<')) this.scan(this.carry + '>');
        this.carry = '';
        this.closeObject();
        return { path: this.path, objects: this.objects, build: this.build };
    }
}

// ------------------------------------------------------------------ Bambu metadata

/** Per object: its filament and its parts' (keyed by the mesh object id the part is). */
export interface ObjectSettings {
    extruder: number;
    name: string;
    parts: Map<string, { extruder: number; subtype: string; name: string }>;
}

/** The `<metadata key="K" value="V"/>` directly in a block of model_settings text. */
function metaValue(block: string, key: string): string | null {
    const re = new RegExp(`<metadata\\s+key="${key}"\\s+value="([^"]*)"`);
    const m = re.exec(block);
    return m ? decodeEntities(m[1]) : null;
}

/** Metadata/model_settings.config -> object id -> the plate it is on (`<plate>` blocks' plater_id). */
export function parsePlateObjects(xml: string): Map<string, number> {
    const out = new Map<string, number>();
    const plateRe = /<plate>([\s\S]*?)<\/plate>/g;
    let m: RegExpExecArray | null;
    while ((m = plateRe.exec(xml)) !== null) {
        const n = Number(metaValue(m[1], 'plater_id'));
        if (!Number.isInteger(n) || n < 1) continue;
        const instRe = /<model_instance>([\s\S]*?)<\/model_instance>/g;
        let i: RegExpExecArray | null;
        while ((i = instRe.exec(m[1])) !== null) {
            const oid = metaValue(i[1], 'object_id');
            if (oid !== null) out.set(oid, n);
        }
    }
    return out;
}

/**
 * Where Bambu Studio puts plate `n` (1-based) of `count` relative to plate 1, in mm: a grid `ceil(√count)` wide,
 * each plate one bed plus a fifth apart, rows going towards -Y (PartPlate.cpp `compute_shape_position`,
 * `plate_stride_x`, `compute_colum_count`).
 */
export function plateOffset(n: number, count: number, bed: LoadedModel['bed']): { x: number; y: number } {
    const root = Math.sqrt(count);
    const cols = root > Math.round(root) ? Math.round(root) + 1 : Math.round(root);
    const i = n - 1;
    return { x: (i % cols) * (bed.maxX - bed.minX) * 1.2, y: -Math.floor(i / cols) * (bed.maxY - bed.minY) * 1.2 };
}

/** The parts on plate `n`, moved onto the bed (a multi-plate project stores later plates beside the first). */
export function partsOnPlate(model: Pick<LoadedModel, 'parts' | 'plates' | 'bed'>, n: number): PreviewPart[] {
    if (model.plates <= 1) return model.parts;
    const { x, y } = plateOffset(n, model.plates, model.bed);
    return model.parts.filter((p) => (p.plate ?? 1) === n).map((p) => {
        if (!x && !y) return p;
        const positions = new Float32Array(p.positions);
        for (let k = 0; k < positions.length; k += 3) {
            positions[k] -= x;
            positions[k + 1] -= y;
        }
        return { ...p, positions };
    });
}

/** Metadata/model_settings.config -> object id -> settings. */
export function parseModelSettings(xml: string): Map<string, ObjectSettings> {
    const out = new Map<string, ObjectSettings>();
    const objRe = /<object\s+id="([^"]*)"[^>]*>([\s\S]*?)<\/object>/g;
    let m: RegExpExecArray | null;
    while ((m = objRe.exec(xml)) !== null) {
        const body = m[2];
        const parts = new Map<string, { extruder: number; subtype: string; name: string }>();
        const partRe = /<part\s+([^>]*)>([\s\S]*?)<\/part>/g;
        let p: RegExpExecArray | null;
        while ((p = partRe.exec(body)) !== null) {
            const id = attr(` ${p[1]}`, 'id') ?? '';
            parts.set(id, {
                extruder: Number(metaValue(p[2], 'extruder') ?? 0) || 0,
                subtype: attr(` ${p[1]}`, 'subtype') ?? 'normal_part',
                name: metaValue(p[2], 'name') ?? ''
            });
        }
        // The object's own metadata sits before its first <part>.
        const head = body.split('<part')[0];
        out.set(m[1], {
            extruder: Number(metaValue(head, 'extruder') ?? 1) || 1,
            name: metaValue(head, 'name') ?? '',
            parts
        });
    }
    return out;
}

type Settings = Record<string, unknown>;

function strList(v: unknown): string[] {
    return Array.isArray(v) ? v.map(String) : typeof v === 'string' ? v.split(';') : [];
}

/** Metadata/project_settings.config (JSON) -> the filaments and the bed. */
export function parseProjectSettings(json: string): {
    filaments: FileFilament[];
    bed: LoadedModel['bed'];
    hasPrintSettings: boolean;
    plate: string | null;
} {
    let s: Settings = {};
    try {
        const v: unknown = JSON.parse(json);
        if (v && typeof v === 'object') s = v as Settings;
    } catch {
        /* not JSON (an old INI-style config): no settings */
    }
    const colours = strList(s.filament_colour);
    const types = strList(s.filament_type);
    const ids = strList(s.filament_ids);
    const n = Math.max(colours.length, types.length);
    const filaments: FileFilament[] = [];
    for (let i = 0; i < n; i++) {
        filaments.push({
            colour: normHex(colours[i]) || NEUTRAL,
            type: types[i] ?? '',
            grams: null,
            profileId: ids[i] ?? ''
        });
    }
    const plate = typeof s.curr_bed_type === 'string' && s.curr_bed_type ? s.curr_bed_type : null;
    return { filaments, bed: bedFromArea(s.printable_area), hasPrintSettings: n > 0, plate };
}

/** printable_area ["0x0","256x0","256x256","0x256"] -> bounds (the default bed when absent). */
export function bedFromArea(area: unknown): LoadedModel['bed'] {
    const pts = strList(area)
        .map((p) => p.split('x').map(Number))
        .filter((p) => p.length === 2 && p.every(Number.isFinite));
    if (pts.length < 3) return { ...DEFAULT_BED };
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const bed = { minX: Math.min(...xs), minY: Math.min(...ys), maxX: Math.max(...xs), maxY: Math.max(...ys) };
    return bed.maxX - bed.minX > 10 && bed.maxY - bed.minY > 10 ? bed : { ...DEFAULT_BED };
}

/** Metadata/slice_info.config -> time, weight and the filaments the plate uses (with grams). */
export function parseSliceInfo(xml: string): { info: SlicedInfo; filaments: Map<number, FileFilament> } | null {
    if (!xml.includes('<plate')) return null;
    const seconds = Number(metaValue(xml, 'prediction') ?? 0) || 0;
    const grams = Number(metaValue(xml, 'weight') ?? 0) || 0;
    const filaments = new Map<number, FileFilament>();
    const re = /<filament\s+([^>]*?)\/?>/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(xml)) !== null) {
        const a = ` ${m[1]}`;
        const id = Number(attr(a, 'id'));
        if (!Number.isInteger(id) || id < 1) continue;
        filaments.set(id, {
            colour: normHex(attr(a, 'color')) || NEUTRAL,
            type: attr(a, 'type') ?? '',
            grams: Number(attr(a, 'used_g') ?? NaN),
            profileId: attr(a, 'tray_info_idx') ?? ''
        });
    }
    return { info: { seconds, grams, used: [...filaments.keys()].sort((a, b) => a - b) }, filaments };
}

// ------------------------------------------------------------------ 3MF assembly

/** The files of a 3MF a preview reads (anything else is skipped while unzipping). */
export function wantedEntry(name: string): boolean {
    return /\.model$/i.test(name) || /^Metadata\/(model_settings|project_settings|slice_info)\.config$/i.test(name);
}

/** The raw text of the small metadata files. */
export interface Metadata {
    modelSettings: string;
    projectSettings: string;
    sliceInfo: string;
    /**
     * The archive holds G-code (`Metadata/plate_N.gcode`): a print file. A project saved after slicing keeps
     * slice_info but no G-code, and is still a project. Undefined: not known (slice_info decides).
     */
    hasGcode?: boolean;
}

/**
 * Put a 3MF together: walk each printable build item's component tree to its meshes and
 * place them on the plate, each with its filament (part extruder, else the object's).
 */
export function assemble3mf(name: string, files: Map<string, ModelFile>, meta: Metadata): LoadedModel {
    const root =
        files.get('3D/3dmodel.model') ?? [...files.values()].find((f) => f.build.length > 0) ?? [...files.values()][0];
    if (!root) throw new Error('this 3MF has no 3D model in it');
    const settings = parseModelSettings(meta.modelSettings);
    const plateOf = parsePlateObjects(meta.modelSettings);
    const plates = Math.max(1, ...plateOf.values());
    const project = parseProjectSettings(meta.projectSettings || '{}');
    const sliced = meta.sliceInfo ? parseSliceInfo(meta.sliceInfo) : null;

    const parts: PreviewPart[] = [];
    let triangles = 0;
    const visit = (path: string, oid: string, xf: Xf, top: ObjectSettings | undefined, depth: number, plate: number, buildOid: string): void => {
        const file = files.get(path);
        const obj = file?.objects.get(oid);
        if (!obj || depth > 8) return;
        if (obj.mesh && obj.mesh.indices.length) {
            const part = top?.parts.get(oid);
            if (part && part.subtype !== 'normal_part') return; // modifiers, blockers: not printed
            const filament = (part?.extruder || top?.extruder || 1) | 0;
            let indices = obj.mesh.indices;
            if (determinant(xf) < 0) {
                // A mirrored placement turns triangles inside out: flip the winding back.
                indices = indices.slice();
                for (let i = 0; i < indices.length; i += 3) {
                    const t = indices[i + 1];
                    indices[i + 1] = indices[i + 2];
                    indices[i + 2] = t;
                }
            }
            // Nested components multiply (a 618-byte zip expanded to 46,656 parts): a budget, not a hang.
            triangles += indices.length / 3;
            if (parts.length >= MAX_PARTS || triangles > MAX_TRIANGLES) {
                throw new Error(`this 3MF places more than ${MAX_PARTS.toLocaleString()} parts or ${MAX_TRIANGLES.toLocaleString()} triangles: too large to preview`);
            }
            parts.push({
                name: part?.name || top?.name || `object ${oid}`,
                filament,
                positions: transformPositions(obj.mesh.positions, xf),
                indices,
                states: obj.mesh.states,
                path,
                objectId: oid,
                buildObjectId: buildOid,
                plate
            });
        }
        for (const c of obj.components) visit(c.path, c.objectId, compose(c.transform, xf), top, depth + 1, plate, buildOid);
    };
    for (const item of root.build) {
        if (!item.printable) continue;
        visit(root.path, item.objectId, item.transform, settings.get(item.objectId), 0, plateOf.get(item.objectId) ?? 1, item.objectId);
    }
    if (!parts.length) throw new Error('this 3MF has no printable objects on its plate');

    let filaments = project.filaments;
    const isSliced = /\.gcode\.3mf$/i.test(name) || (meta.hasGcode ?? !!sliced);
    if (sliced && isSliced) {
        // A sliced file's own record of each filament (with grams) wins over the project list.
        const n = Math.max(filaments.length, ...sliced.filaments.keys());
        filaments = Array.from({ length: n }, (_, i) =>
            sliced.filaments.get(i + 1) ?? filaments[i] ?? { colour: NEUTRAL, type: '', grams: null, profileId: '' }
        );
    }
    const kind: ModelKind = isSliced || /\.gcode\.3mf$/i.test(name) ? 'sliced' : project.hasPrintSettings ? 'project' : 'geometry';
    if (kind === 'geometry') {
        // No project: one filament the user picks; place it on the bed like a mesh when it is off the plate.
        for (const p of parts) p.filament = 1;
        filaments = [{ colour: NEUTRAL, type: '', grams: null, profileId: '' }];
        placeOnBed(parts, project.bed, false);
    }
    if (!filaments.length) filaments = [{ colour: NEUTRAL, type: '', grams: null, profileId: '' }];
    return {
        name,
        kind,
        parts,
        filaments,
        bed: project.bed,
        triangles: countTriangles(parts),
        sliced: isSliced ? (sliced?.info ?? null) : null,
        plate: project.plate,
        plates: kind === 'project' ? plates : 1
    };
}

// ------------------------------------------------------------------ STL / OBJ

/** Bounds of the parts' vertices. */
export function bounds(parts: PreviewPart[]): { min: [number, number, number]; max: [number, number, number] } {
    const min: [number, number, number] = [Infinity, Infinity, Infinity];
    const max: [number, number, number] = [-Infinity, -Infinity, -Infinity];
    for (const p of parts) {
        const a = p.positions;
        for (let i = 0; i < a.length; i += 3) {
            for (let k = 0; k < 3; k++) {
                const v = a[i + k];
                if (v < min[k]) min[k] = v;
                if (v > max[k]) max[k] = v;
            }
        }
    }
    return { min, max };
}

/**
 * Move parts onto the bed: centred in X/Y and standing on Z = 0. With `always` false, parts
 * whose centre already lies on the bed (and not below it) stay where the file put them.
 */
export function placeOnBed(parts: PreviewPart[], bed: LoadedModel['bed'], always = true): void {
    const { min, max } = bounds(parts);
    if (!Number.isFinite(min[0])) return;
    const cx = (min[0] + max[0]) / 2;
    const cy = (min[1] + max[1]) / 2;
    const onBed = cx >= bed.minX && cx <= bed.maxX && cy >= bed.minY && cy <= bed.maxY && min[2] > -0.5;
    if (!always && onBed) return;
    const dx = (bed.minX + bed.maxX) / 2 - cx;
    const dy = (bed.minY + bed.maxY) / 2 - cy;
    const dz = -min[2];
    for (const p of parts) {
        const a = p.positions;
        for (let i = 0; i < a.length; i += 3) {
            a[i] += dx;
            a[i + 1] += dy;
            a[i + 2] += dz;
        }
    }
}

export function countTriangles(parts: PreviewPart[]): number {
    return parts.reduce((n, p) => n + (p.indices ? p.indices.length : p.positions.length / 3) / 3, 0);
}

/** A binary or ASCII STL -> one non-indexed part. */
export function parseStl(bytes: Uint8Array): Float32Array {
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (bytes.byteLength >= 84) {
        const n = dv.getUint32(80, true);
        if (84 + n * 50 === bytes.byteLength) {
            const out = new Float32Array(n * 9);
            for (let t = 0; t < n; t++) {
                const base = 84 + t * 50 + 12; // skip the facet normal
                for (let k = 0; k < 9; k++) out[t * 9 + k] = dv.getFloat32(base + k * 4, true);
            }
            return out;
        }
    }
    const text = new TextDecoder().decode(bytes);
    if (!/\bfacet\b/.test(text)) throw new Error('this is not a readable STL file');
    const re = /vertex\s+(\S+)\s+(\S+)\s+(\S+)/g;
    const g = new Grow((n) => new Float32Array(n));
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
        g.push(Number(m[1]));
        g.push(Number(m[2]));
        g.push(Number(m[3]));
    }
    const out = g.done();
    return out.length % 9 === 0 ? out : out.slice(0, out.length - (out.length % 9));
}

/** A Wavefront OBJ (v / f lines; polygons fan-triangulated; negative indices) -> one indexed mesh. */
export function parseObj(text: string): { positions: Float32Array; indices: Uint32Array } {
    const v = new Grow((n) => new Float32Array(n));
    const f = new Grow((n) => new Uint32Array(n));
    let count = 0;
    for (const raw of text.split('\n')) {
        const line = raw.trim();
        if (line.startsWith('v ')) {
            const p = line.split(/\s+/);
            v.push(Number(p[1]));
            v.push(Number(p[2]));
            v.push(Number(p[3]));
            count += 1;
        } else if (line.startsWith('f ')) {
            const ids = line
                .split(/\s+/)
                .slice(1)
                .map((tok) => {
                    const i = parseInt(tok.split('/')[0], 10);
                    return i < 0 ? count + i : i - 1;
                })
                .filter((i) => Number.isInteger(i) && i >= 0);
            for (let k = 1; k + 1 < ids.length; k++) {
                f.push(ids[0]);
                f.push(ids[k]);
                f.push(ids[k + 1]);
            }
        }
    }
    if (!f.len) throw new Error('this OBJ file has no faces');
    return { positions: v.done(), indices: f.done() };
}

/** An STL / OBJ file as a loaded model on the default bed, one filament. */
export function meshModel(name: string, positions: Float32Array, indices: Uint32Array | null): LoadedModel {
    const parts: PreviewPart[] = [{ name, filament: 1, positions, indices, states: null }];
    placeOnBed(parts, DEFAULT_BED, true);
    return {
        name,
        kind: 'mesh',
        parts,
        filaments: [{ colour: NEUTRAL, type: '', grams: null, profileId: '' }],
        bed: { ...DEFAULT_BED },
        triangles: countTriangles(parts),
        sliced: null,
        plate: null,
        plates: 1
    };
}

// ------------------------------------------------------------------ normals

/**
 * Per-vertex normals: area-weighted averages of the triangles sharing a vertex (smooth
 * shading for an indexed mesh), or each triangle's own normal on a non-indexed one (flat,
 * as STL facets should look). Computed off the main thread so a 2M-triangle figure does not
 * freeze the page while three.js would do the same.
 */
export function computeNormals(positions: Float32Array, indices: Uint32Array | null): Float32Array {
    const n = new Float32Array(positions.length);
    const tris = indices ? indices.length / 3 : positions.length / 9;
    for (let t = 0; t < tris; t++) {
        const a = indices ? indices[t * 3] : t * 3;
        const b = indices ? indices[t * 3 + 1] : t * 3 + 1;
        const c = indices ? indices[t * 3 + 2] : t * 3 + 2;
        const ax = positions[a * 3], ay = positions[a * 3 + 1], az = positions[a * 3 + 2];
        const e1x = positions[b * 3] - ax, e1y = positions[b * 3 + 1] - ay, e1z = positions[b * 3 + 2] - az;
        const e2x = positions[c * 3] - ax, e2y = positions[c * 3 + 1] - ay, e2z = positions[c * 3 + 2] - az;
        // Cross product: its length is twice the triangle's area, which weights the average.
        const nx = e1y * e2z - e1z * e2y;
        const ny = e1z * e2x - e1x * e2z;
        const nz = e1x * e2y - e1y * e2x;
        for (const v of [a, b, c]) {
            n[v * 3] += nx;
            n[v * 3 + 1] += ny;
            n[v * 3 + 2] += nz;
        }
    }
    for (let i = 0; i < n.length; i += 3) {
        const len = Math.hypot(n[i], n[i + 1], n[i + 2]) || 1;
        n[i] /= len;
        n[i + 1] /= len;
        n[i + 2] /= len;
    }
    return n;
}

/** The model with every part's normals filled in. */
export function withNormals(model: LoadedModel): LoadedModel {
    for (const p of model.parts) p.normals = computeNormals(p.positions, p.indices);
    return model;
}

// ------------------------------------------------------------------ colouring

/**
 * One RGB byte triple per vertex of a part: its filament colour, with painted triangles in
 * their painted filament's colour. On an indexed mesh a vertex shared by differently painted
 * triangles takes the painted one (painted triangles are written last), so boundaries are
 * soft by one vertex ring — fine for a preview, and a quarter of the memory of unshared vertices.
 */
export function vertexColours(part: PreviewPart, palette: string[]): Uint8Array {
    const rgb = palette.map((c) => {
        const h = normHex(c) || NEUTRAL;
        return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    });
    const base = rgb[part.filament - 1] ?? rgb[0] ?? [200, 200, 200];
    const nVerts = part.positions.length / 3;
    const out = new Uint8Array(nVerts * 3);
    for (let i = 0; i < nVerts; i++) {
        out[i * 3] = base[0];
        out[i * 3 + 1] = base[1];
        out[i * 3 + 2] = base[2];
    }
    const states = part.states;
    if (!states) return out;
    const idx = part.indices;
    for (let t = 0; t < states.length; t++) {
        const st = states[t];
        if (!st) continue;
        const c = rgb[st - 1] ?? base;
        for (let k = 0; k < 3; k++) {
            const v = idx ? idx[t * 3 + k] : t * 3 + k;
            out[v * 3] = c[0];
            out[v * 3 + 1] = c[1];
            out[v * 3 + 2] = c[2];
        }
    }
    return out;
}
