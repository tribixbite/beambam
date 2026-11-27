/**
 * Paint edits over a loaded model: what the user painted, applied live to the preview's per-triangle
 * state buffers and collected into a {@link PaintMap} for the 3MF writer at slice time.
 *
 * Only the triangles the user touches are recorded, so a file's existing paint on untouched triangles
 * is preserved (the writer leaves those alone). Paint lives on the object mesh, so every placed
 * instance of an object shares one live state buffer and repaints together.
 *
 * See docs/PAINT_PLAN.md.
 */
import type { PreviewPart } from './model';
import type { PaintMap } from './paint3mf';
import { regionAround, type PaintMode } from './paintregion';

/** Key for an object mesh: its model entry path and `<object>` id. */
export function partKey(part: Pick<PreviewPart, 'path' | 'objectId'>): string {
    return `${part.path ?? ''}\u0000${part.objectId ?? ''}`;
}

/** A part can be painted only when it comes from a 3MF object with indexed geometry. */
export function paintable(part: PreviewPart): boolean {
    return !!part.path && !!part.objectId && !!part.indices && part.indices.length > 0;
}

/** One triangle's full transition, enough to undo or redo it. */
interface Edit {
    key: string;
    tri: number;
    beforeOverride: number | undefined;
    afterOverride: number | undefined;
    beforeState: number;
    afterState: number;
}

export class PaintSession {
    /** The user's edits: object key -> (triangle index -> filament state; 0 clears to the part colour). */
    private readonly overrides = new Map<string, Map<number, number>>();
    /** Live preview state buffer per object (shared by every instance), mutated as the user paints. */
    private readonly live = new Map<string, Uint8Array>();
    /** The model's original per-triangle state, never mutated (for clear / undo-to-original). */
    private readonly base = new Map<string, Uint8Array>();
    private readonly geom = new Map<string, { positions: Float32Array; indices: Uint32Array }>();
    private readonly undoStack: Edit[][] = [];
    private readonly redoStack: Edit[][] = [];

    constructor(parts: PreviewPart[]) {
        for (const p of parts) {
            if (!paintable(p)) continue;
            const key = partKey(p);
            const triCount = (p.indices as Uint32Array).length / 3;
            if (!this.live.has(key)) {
                // The object's own state array is the live buffer, mutated in place so the preview part (and
                // any plate copy sharing it by reference) shows paint without a rebuild; `base` is a pristine
                // snapshot for clear / undo-to-original.
                const live = p.states && p.states.length === triCount ? p.states : new Uint8Array(triCount);
                this.base.set(key, live.slice());
                this.live.set(key, live);
                this.geom.set(key, { positions: p.positions, indices: p.indices as Uint32Array });
            }
            p.states = this.live.get(key)!; // every instance (incl. a freshly allocated buffer) shares it
        }
    }

    /** Object keys that can be painted (have geometry). */
    keys(): string[] {
        return [...this.live.keys()];
    }

    /** True while anything is painted (and not fully undone). */
    get dirty(): boolean {
        for (const m of this.overrides.values()) if (m.size) return true;
        return false;
    }

    get canUndo(): boolean {
        return this.undoStack.length > 0;
    }
    get canRedo(): boolean {
        return this.redoStack.length > 0;
    }

    /** Filament numbers currently painted anywhere (for extending the palette / spool list). */
    usedFilaments(): Set<number> {
        const out = new Set<number>();
        for (const m of this.overrides.values()) for (const s of m.values()) if (s > 0) out.add(s);
        return out;
    }

    private apply(e: Edit, forward: boolean): void {
        const ov = this.overrides.get(e.key) ?? new Map<number, number>();
        const nextOverride = forward ? e.afterOverride : e.beforeOverride;
        if (nextOverride === undefined) ov.delete(e.tri);
        else ov.set(e.tri, nextOverride);
        this.overrides.set(e.key, ov);
        this.live.get(e.key)![e.tri] = forward ? e.afterState : e.beforeState;
    }

    private commit(edits: Edit[]): boolean {
        if (!edits.length) return false;
        for (const e of edits) this.apply(e, true);
        this.undoStack.push(edits);
        this.redoStack.length = 0;
        return true;
    }

    /**
     * Paint the region a tap on `part`'s triangle `face` selects, in `mode`, with filament `filament`
     * (0 clears to the part's own colour). Returns true when any triangle changed.
     */
    paint(part: PreviewPart, face: number, mode: PaintMode, filament: number, creaseDeg = 30): boolean {
        if (!paintable(part)) return false;
        const key = partKey(part);
        const g = this.geom.get(key);
        const buf = this.live.get(key);
        if (!g || !buf) return false;
        const ov = this.overrides.get(key);
        const edits: Edit[] = [];
        for (const tri of regionAround(g, face, mode, creaseDeg)) {
            if (buf[tri] === filament) continue;
            edits.push({
                key,
                tri,
                beforeOverride: ov?.get(tri),
                afterOverride: filament,
                beforeState: buf[tri],
                afterState: filament
            });
        }
        return this.commit(edits);
    }

    /** Clear every edit as one undoable step, restoring each touched triangle to its original state. */
    clear(): boolean {
        const edits: Edit[] = [];
        for (const [key, ov] of this.overrides) {
            const buf = this.live.get(key)!;
            const orig = this.base.get(key)!;
            for (const [tri, cur] of ov) {
                edits.push({
                    key,
                    tri,
                    beforeOverride: cur,
                    afterOverride: undefined,
                    beforeState: buf[tri],
                    afterState: orig[tri]
                });
            }
        }
        return this.commit(edits);
    }

    undo(): boolean {
        const edits = this.undoStack.pop();
        if (!edits) return false;
        for (const e of edits) this.apply(e, false);
        this.redoStack.push(edits);
        return true;
    }

    redo(): boolean {
        const edits = this.redoStack.pop();
        if (!edits) return false;
        for (const e of edits) this.apply(e, true);
        this.undoStack.push(edits);
        return true;
    }

    /** The edits as a PaintMap the 3MF writer consumes (only touched triangles). */
    toPaintMap(): PaintMap {
        const map: PaintMap = new Map();
        for (const [key, ov] of this.overrides) {
            if (!ov.size) continue;
            const sep = key.indexOf('\u0000');
            const path = key.slice(0, sep);
            const objectId = key.slice(sep + 1);
            let objs = map.get(path);
            if (!objs) {
                objs = new Map();
                map.set(path, objs);
            }
            objs.set(objectId, new Map(ov));
        }
        return map;
    }
}
