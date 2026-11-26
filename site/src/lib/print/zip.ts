/**
 * A minimal random-access ZIP reader over a Blob: read the central directory from the end
 * of the file, then stream single entries through the browser's native
 * `DecompressionStream('deflate-raw')`. Native inflate is several times faster than a JS
 * inflater on a phone (a 172 MB model entry: ~5 s in fflate), and Blob.slice means only the
 * entries asked for are read. ZIP64 sizes and offsets are understood.
 */

export interface ZipEntry {
    name: string;
    /** 0 = stored, 8 = deflate. */
    method: number;
    compressedSize: number;
    size: number;
    localOffset: number;
}

const EOCD_SIG = 0x06054b50;
const ZIP64_LOCATOR_SIG = 0x07064b50;
const ZIP64_EOCD_SIG = 0x06064b50;
const CENTRAL_SIG = 0x02014b50;
const LOCAL_SIG = 0x04034b50;

async function bytes(blob: Blob, start: number, end: number): Promise<DataView> {
    const buf = await blob.slice(start, end).arrayBuffer();
    return new DataView(buf);
}

function u64(dv: DataView, off: number): number {
    return dv.getUint32(off, true) + dv.getUint32(off + 4, true) * 2 ** 32;
}

/** Native raw-deflate streams exist (all current browsers; bun). */
export function hasNativeInflate(): boolean {
    try {
        return typeof DecompressionStream !== 'undefined' && !!new DecompressionStream('deflate-raw' as CompressionFormat);
    } catch {
        return false;
    }
}

/** The entries of a ZIP, from its central directory. Throws when the Blob is not a ZIP. */
export async function listEntries(blob: Blob): Promise<ZipEntry[]> {
    const tailLen = Math.min(blob.size, 65557 + 20);
    const tail = await bytes(blob, blob.size - tailLen, blob.size);
    let eocd = -1;
    for (let i = tail.byteLength - 22; i >= 0; i--) {
        if (tail.getUint32(i, true) === EOCD_SIG) {
            eocd = i;
            break;
        }
    }
    if (eocd < 0) throw new Error('this file is not a ZIP / 3MF archive');
    let count = tail.getUint16(eocd + 10, true);
    let cdSize = tail.getUint32(eocd + 12, true);
    let cdOffset = tail.getUint32(eocd + 16, true);
    if ((cdOffset === 0xffffffff || count === 0xffff) && eocd >= 20 && tail.getUint32(eocd - 20, true) === ZIP64_LOCATOR_SIG) {
        const z64Offset = u64(tail, eocd - 20 + 8);
        const z = await bytes(blob, z64Offset, z64Offset + 56);
        if (z.getUint32(0, true) === ZIP64_EOCD_SIG) {
            count = u64(z, 32);
            cdSize = u64(z, 40);
            cdOffset = u64(z, 48);
        }
    }
    const cd = await bytes(blob, cdOffset, cdOffset + cdSize);
    const dec = new TextDecoder();
    const out: ZipEntry[] = [];
    let p = 0;
    for (let n = 0; n < count && p + 46 <= cd.byteLength; n++) {
        if (cd.getUint32(p, true) !== CENTRAL_SIG) throw new Error('damaged ZIP central directory');
        const method = cd.getUint16(p + 10, true);
        let compressedSize = cd.getUint32(p + 20, true);
        let size = cd.getUint32(p + 24, true);
        const nameLen = cd.getUint16(p + 28, true);
        const extraLen = cd.getUint16(p + 30, true);
        const commentLen = cd.getUint16(p + 32, true);
        let localOffset = cd.getUint32(p + 42, true);
        const name = dec.decode(new Uint8Array(cd.buffer, cd.byteOffset + p + 46, nameLen));
        // ZIP64 extra field: the 0xFFFFFFFF fields follow in order size, compressed size, offset.
        let e = p + 46 + nameLen;
        const eEnd = e + extraLen;
        while (e + 4 <= eEnd) {
            const id = cd.getUint16(e, true);
            const len = cd.getUint16(e + 2, true);
            if (id === 0x0001) {
                let q = e + 4;
                if (size === 0xffffffff) { size = u64(cd, q); q += 8; }
                if (compressedSize === 0xffffffff) { compressedSize = u64(cd, q); q += 8; }
                if (localOffset === 0xffffffff) { localOffset = u64(cd, q); }
            }
            e += 4 + len;
        }
        out.push({ name, method, compressedSize, size, localOffset });
        p += 46 + nameLen + extraLen + commentLen;
    }
    return out;
}

/** The decompressed bytes of one entry, as a stream. */
export async function entryStream(blob: Blob, entry: ZipEntry): Promise<ReadableStream<Uint8Array>> {
    const head = await bytes(blob, entry.localOffset, entry.localOffset + 30);
    if (head.getUint32(0, true) !== LOCAL_SIG) throw new Error(`damaged ZIP entry ${entry.name}`);
    const start = entry.localOffset + 30 + head.getUint16(26, true) + head.getUint16(28, true);
    const raw = blob.slice(start, start + entry.compressedSize).stream() as ReadableStream<Uint8Array>;
    if (entry.method === 0) return raw;
    if (entry.method !== 8) throw new Error(`${entry.name}: unsupported ZIP compression (${entry.method})`);
    const inflate = new DecompressionStream('deflate-raw' as CompressionFormat) as unknown as ReadableWritablePair<
        Uint8Array,
        Uint8Array
    >;
    return raw.pipeThrough(inflate);
}
