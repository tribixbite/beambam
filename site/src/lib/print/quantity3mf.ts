/**
 * Rewrite a 3MF's build items so each object is printed a chosen number of times, each copy translated to
 * a packed position on the plate (the per-object quantity feature). Only the root model's `<build>` is
 * touched; the meshes are shared by the copies (as instances). Copies get fresh UUIDs so the file stays
 * valid. See arrange.ts for the packing.
 */
import { strFromU8, strToU8, unzipSync, zipSync } from 'fflate';

import { attr } from './model';

/** Per object id: one XY offset (added to the item's translation) for each wanted copy. Empty = drop it. */
export type QuantitySpec = Map<string, Array<{ dx: number; dy: number }>>;

const ITEM = /<item\b[^>]*?\/>/g;
const BUILD = /<build\b[^>]*>[\s\S]*?<\/build>/;
const IDENTITY = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0];

function uuid(): string {
    // A plain v4-ish UUID (crypto.randomUUID isn't everywhere); good enough for a 3MF p:UUID.
    const h = '0123456789abcdef';
    let s = '';
    for (let i = 0; i < 32; i++) s += h[(Math.random() * 16) | 0];
    return `${s.slice(0, 8)}-${s.slice(8, 12)}-4${s.slice(13, 16)}-8${s.slice(17, 20)}-${s.slice(20, 32)}`;
}

function transformOf(tag: string): number[] {
    const t = attr(tag, 'transform');
    if (!t) return [...IDENTITY];
    const nums = t.trim().split(/\s+/).map(Number);
    return nums.length === 12 && nums.every((n) => Number.isFinite(n)) ? nums : [...IDENTITY];
}

/** `tag` with its transform translated by (dx, dy) and a fresh p:UUID. */
function copyItem(tag: string, dx: number, dy: number): string {
    const m = transformOf(tag);
    m[9] += dx;
    m[10] += dy;
    const xf = m.map((n) => (Number.isInteger(n) ? String(n) : `${n}`)).join(' ');
    let out = tag.includes('transform="')
        ? tag.replace(/transform="[^"]*"/, `transform="${xf}"`)
        : tag.replace(/\/>$/, ` transform="${xf}"/>`);
    out = out.includes('p:UUID="')
        ? out.replace(/p:UUID="[^"]*"/, `p:UUID="${uuid()}"`)
        : out.replace(/\/>$/, ` p:UUID="${uuid()}"/>`);
    return out;
}

/** Rewrite the `<build>` of one model XML so each object yields its wanted copies. */
export function rewriteBuild(xml: string, spec: QuantitySpec): string {
    return xml.replace(BUILD, (build) => {
        const done = new Set<string>(); // object ids already emitted (drop any further original items for them)
        return build.replace(ITEM, (item) => {
            const oid = attr(item, 'objectid') ?? '';
            const offs = spec.get(oid);
            if (!offs) return item; // object not in the spec: leave it
            if (done.has(oid)) return ''; // its copies were already emitted on the first item
            done.add(oid);
            // The first copy keeps the original UUID; the rest get fresh ones.
            return offs
                .map((o, i) => (i === 0 ? copyItemKeepUuid(item, o.dx, o.dy) : copyItem(item, o.dx, o.dy)))
                .join('\n   ');
        });
    });
}

/** Like copyItem but keeps the original UUID (for the first copy). */
function copyItemKeepUuid(tag: string, dx: number, dy: number): string {
    const m = transformOf(tag);
    m[9] += dx;
    m[10] += dy;
    const xf = m.join(' ');
    return tag.includes('transform="')
        ? tag.replace(/transform="[^"]*"/, `transform="${xf}"`)
        : tag.replace(/\/>$/, ` transform="${xf}"/>`);
}

/** True when the spec changes any object's count (so a rewrite is needed). */
export function hasQuantityChange(spec: QuantitySpec, baseCount: Map<string, number>): boolean {
    for (const [oid, offs] of spec) if (offs.length !== (baseCount.get(oid) ?? 1)) return true;
    return false;
}

/** A copy of `original` with the build rewritten for `spec`. Rewrites every `.model` entry that has a build. */
export async function writeQuantity3mf(original: Blob, spec: QuantitySpec): Promise<Blob> {
    const buf = new Uint8Array(await original.arrayBuffer());
    const entries = unzipSync(buf);
    for (const [path, data] of Object.entries(entries)) {
        if (!/\.model$/i.test(path)) continue;
        const xml = strFromU8(data);
        if (!BUILD.test(xml)) continue;
        entries[path] = strToU8(rewriteBuild(xml, spec));
    }
    const out = zipSync(entries, { level: 6 });
    return new Blob([out as BlobPart], { type: 'application/vnd.ms-package.3dmanufacturing-3dmodel+xml' });
}
