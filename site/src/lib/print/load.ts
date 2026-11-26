/**
 * Read a dropped file into a `LoadedModel`. 3MF entries are streamed straight into the model
 * scanners, so a 29 MB project holding 172 MB of model XML never sits in memory whole: with
 * the browser's native inflater when it has one (zip.ts), else fflate's streaming `Unzip`.
 * Runs in the preview worker; also callable directly (tests).
 */
import { Unzip, UnzipInflate, unzipSync } from 'fflate';
import {
    assemble3mf,
    meshModel,
    ModelScanner,
    parseObj,
    parseStl,
    wantedEntry,
    type LoadedModel,
    type Metadata,
    type ModelFile
} from './model';
import { entryStream, hasNativeInflate, listEntries } from './zip';

/** What kind of file a name is. */
export type FileType = '3mf' | 'gcode.3mf' | 'stl' | 'obj' | 'unknown';

export function fileType(name: string): FileType {
    const n = name.toLowerCase();
    if (n.endsWith('.gcode.3mf')) return 'gcode.3mf';
    if (n.endsWith('.3mf')) return '3mf';
    if (n.endsWith('.stl')) return 'stl';
    if (n.endsWith('.obj')) return 'obj';
    return 'unknown';
}

/** Progress: a 0..1 fraction and what is happening. */
export type Progress = (fraction: number, what: string) => void;

/** Collects the wanted entries of a 3MF as they are inflated. */
class Collector {
    readonly scanners = new Map<string, { scan: ModelScanner; dec: TextDecoder }>();
    readonly texts = new Map<string, { parts: string[]; dec: TextDecoder }>();

    /** A new entry: returns a chunk sink, or null to skip it. */
    open(name: string): ((chunk: Uint8Array, final: boolean) => void) | null {
        if (!wantedEntry(name)) return null;
        if (/\.model$/i.test(name)) {
            const entry = { scan: new ModelScanner(name), dec: new TextDecoder() };
            this.scanners.set(name, entry);
            return (chunk, final) => {
                entry.scan.push(entry.dec.decode(chunk, { stream: !final }));
            };
        }
        const entry = { parts: [] as string[], dec: new TextDecoder() };
        this.texts.set(name, entry);
        return (chunk, final) => {
            entry.parts.push(entry.dec.decode(chunk, { stream: !final }));
        };
    }

    result(name: string): LoadedModel {
        const files = new Map<string, ModelFile>();
        for (const [path, e] of this.scanners) files.set(path, e.scan.end());
        const text = (re: RegExp): string => {
            for (const [k, v] of this.texts) if (re.test(k)) return v.parts.join('');
            return '';
        };
        const meta: Metadata = {
            modelSettings: text(/model_settings\.config$/i),
            projectSettings: text(/project_settings\.config$/i),
            sliceInfo: text(/slice_info\.config$/i)
        };
        return assemble3mf(name, files, meta);
    }
}

/** Feed a whole entry's bytes to a sink in slices (keeps decoded strings small). */
function feed(sink: (c: Uint8Array, f: boolean) => void, data: Uint8Array): void {
    const step = 4 << 20;
    if (!data.length) sink(data, true);
    for (let i = 0; i < data.length; i += step) sink(data.subarray(i, i + step), i + step >= data.length);
}

/** Unzip a whole buffer at once: the fallback for archives the stream reader cannot follow. */
function read3mfBuffer(name: string, bytes: Uint8Array): LoadedModel {
    const c = new Collector();
    const entries = unzipSync(bytes, { filter: (f) => wantedEntry(f.name) });
    for (const [entry, data] of Object.entries(entries)) {
        const sink = c.open(entry);
        if (sink) feed(sink, data);
    }
    return c.result(name);
}

/** Read the wanted entries with random access + native inflate. */
async function read3mfNative(name: string, blob: Blob, progress?: Progress): Promise<LoadedModel> {
    const c = new Collector();
    const entries = (await listEntries(blob)).filter((e) => wantedEntry(e.name));
    if (!entries.some((e) => /\.model$/i.test(e.name))) throw new Error('this 3MF has no 3D model in it');
    const total = entries.reduce((n, e) => n + e.size, 0) || 1;
    let done = 0;
    for (const e of entries) {
        const sink = c.open(e.name);
        if (!sink) continue;
        const reader = (await entryStream(blob, e)).getReader();
        for (;;) {
            const { done: end, value } = await reader.read();
            if (end) {
                sink(new Uint8Array(0), true);
                break;
            }
            sink(value, false);
            done += value.byteLength;
            progress?.(done / total, 'Reading the model');
        }
    }
    return c.result(name);
}

/** Stream a 3MF Blob through fflate's unzipper (no native inflater). Exported for tests. */
export async function read3mf(name: string, blob: Blob, progress?: Progress): Promise<LoadedModel> {
    const c = new Collector();
    let failed: Error | null = null;
    const uz = new Unzip((file) => {
        const sink = c.open(file.name);
        if (!sink) return; // not started: its bytes are skipped
        file.ondata = (err, chunk, final) => {
            if (err) {
                failed = err;
                return;
            }
            sink(chunk, final);
        };
        file.start();
    });
    uz.register(UnzipInflate);
    const reader = blob.stream().getReader();
    let read = 0;
    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (done) {
                uz.push(new Uint8Array(0), true);
                break;
            }
            read += value.byteLength;
            uz.push(value, false);
            progress?.(read / blob.size, 'Reading the model');
            if (failed) throw failed;
        }
        if (failed) throw failed;
        if (!c.scanners.size) throw new Error('no model entries found while streaming');
        return c.result(name);
    } catch (e) {
        // Some writers put sizes only after the data (or use ZIP64): read the archive whole instead.
        if (e instanceof Error && /no printable|no 3D model/.test(e.message)) throw e;
        progress?.(1, 'Reading the model (whole archive)');
        return read3mfBuffer(name, new Uint8Array(await blob.arrayBuffer()));
    }
}

/** Read any supported file. Throws an Error with a plain-words message for anything else. */
export async function loadModel(name: string, blob: Blob, progress?: Progress): Promise<LoadedModel> {
    const type = fileType(name);
    if (type === '3mf' || type === 'gcode.3mf') {
        return hasNativeInflate() ? read3mfNative(name, blob, progress) : read3mf(name, blob, progress);
    }
    if (type === 'stl') {
        progress?.(0.2, 'Reading the STL');
        return meshModel(name, parseStl(new Uint8Array(await blob.arrayBuffer())), null);
    }
    if (type === 'obj') {
        progress?.(0.2, 'Reading the OBJ');
        const { positions, indices } = parseObj(await blob.text());
        return meshModel(name, positions, indices);
    }
    throw new Error(`${name}: beambam prints .3mf, .gcode.3mf, .stl and .obj files`);
}
