/// <reference lib="webworker" />
/**
 * Off-main-thread model reading: the page posts {id, name, file}; the worker answers with
 * progress messages and then the `LoadedModel`, its typed arrays transferred (not copied).
 */
import { loadModel } from './load';
import { withNormals, type LoadedModel } from './model';

export type WorkerRequest = { id: number; name: string; file: Blob };
export type WorkerReply =
    | { id: number; type: 'progress'; fraction: number; what: string }
    | { id: number; type: 'done'; model: LoadedModel }
    | { id: number; type: 'error'; message: string };

const ctx = self as unknown as DedicatedWorkerGlobalScope;

ctx.onmessage = async (ev: MessageEvent<WorkerRequest>) => {
    const { id, name, file } = ev.data;
    let last = 0;
    try {
        const loaded = await loadModel(name, file, (fraction, what) => {
            const now = Date.now();
            if (now - last < 120) return; // a few updates per second is plenty
            last = now;
            ctx.postMessage({ id, type: 'progress', fraction, what } satisfies WorkerReply);
        });
        ctx.postMessage({ id, type: 'progress', fraction: 1, what: 'Shading the model' } satisfies WorkerReply);
        const model = withNormals(loaded);
        const transfer: Transferable[] = [];
        for (const p of model.parts) {
            transfer.push(p.positions.buffer as ArrayBuffer);
            if (p.indices && p.indices.buffer !== p.positions.buffer) transfer.push(p.indices.buffer as ArrayBuffer);
            if (p.states) transfer.push(p.states.buffer as ArrayBuffer);
            if (p.normals) transfer.push(p.normals.buffer as ArrayBuffer);
        }
        // A mesh instanced twice shares its index/state buffers: transfer each buffer once.
        ctx.postMessage({ id, type: 'done', model } satisfies WorkerReply, [...new Set(transfer)] as Transferable[]);
    } catch (e) {
        ctx.postMessage({ id, type: 'error', message: e instanceof Error ? e.message : String(e) } satisfies WorkerReply);
    }
};
