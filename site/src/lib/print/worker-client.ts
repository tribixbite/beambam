/**
 * The page's side of the preview worker: one worker reused for every file, each request
 * matched to its answer by id (the page ignores a result for a file it no longer shows). Falls back to reading on the main thread
 * when workers are unavailable.
 */
import type { LoadedModel } from './model';
import type { PaintMap } from './paint3mf';
import type { WorkerReply, WorkerRequest } from './preview.worker';

let worker: Worker | null = null;
let seq = 0;

function getWorker(): Worker | null {
    if (worker) return worker;
    try {
        worker = new Worker(new URL('./preview.worker.ts', import.meta.url), { type: 'module' });
    } catch {
        worker = null;
    }
    return worker;
}

/** Read `file` into a model (normals included), reporting progress. */
export async function readModelFile(file: File, onProgress: (fraction: number, what: string) => void): Promise<LoadedModel> {
    const id = ++seq;
    const w = getWorker();
    if (!w) {
        const { loadModel } = await import('./load');
        const { withNormals } = await import('./model');
        return withNormals(await loadModel(file.name, file, onProgress));
    }
    return new Promise<LoadedModel>((resolve, reject) => {
        const onMessage = (ev: MessageEvent<WorkerReply>): void => {
            const r = ev.data;
            if (r.id !== id) return; // another request's answer
            if (r.type === 'progress') {
                onProgress(r.fraction, r.what);
                return;
            }
            w.removeEventListener('message', onMessage);
            w.removeEventListener('error', onError);
            if (r.type === 'done') resolve(r.model);
            else if (r.type === 'error') reject(new Error(r.message));
            else reject(new Error('unexpected worker reply'));
        };
        const onError = (ev: ErrorEvent): void => {
            w.removeEventListener('message', onMessage);
            w.removeEventListener('error', onError);
            worker?.terminate();
            worker = null;
            reject(new Error(ev.message || 'the preview worker failed (the file may be too large for this device)'));
        };
        w.addEventListener('message', onMessage);
        w.addEventListener('error', onError);
        w.postMessage({ id, name: file.name, file } satisfies WorkerRequest);
    });
}

/**
 * Bake `paint` into `file` (a 3MF) off the main thread, so re-zipping a large model does not freeze the
 * tab. Falls back to the main thread when workers are unavailable.
 */
export async function serializePainted(file: Blob, paint: PaintMap): Promise<Blob> {
    const w = getWorker();
    if (!w) {
        const { writePainted3mf } = await import('./paint3mf');
        return writePainted3mf(file, paint);
    }
    const id = ++seq;
    return new Promise<Blob>((resolve, reject) => {
        const onMessage = (ev: MessageEvent<WorkerReply>): void => {
            const r = ev.data;
            if (r.id !== id) return;
            if (r.type === 'progress') return;
            w.removeEventListener('message', onMessage);
            w.removeEventListener('error', onError);
            if (r.type === 'painted') resolve(r.blob);
            else if (r.type === 'error') reject(new Error(r.message));
            else reject(new Error('unexpected worker reply'));
        };
        const onError = (ev: ErrorEvent): void => {
            w.removeEventListener('message', onMessage);
            w.removeEventListener('error', onError);
            worker?.terminate();
            worker = null;
            reject(new Error(ev.message || 'the preview worker failed while baking the paint'));
        };
        w.addEventListener('message', onMessage);
        w.addEventListener('error', onError);
        w.postMessage({ id, type: 'paint', file, paint } satisfies WorkerRequest);
    });
}
