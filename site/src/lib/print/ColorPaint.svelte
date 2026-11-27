<script lang="ts" module>
    import type { PaintMap } from './paint3mf';
    import type { SpoolOption } from './plan';

    /** What the panel reports to the page after every paint change. */
    export interface PaintInfo {
        /** Any triangle is painted (not fully undone). */
        dirty: boolean;
        /** Colour of every filament, base then painted (index 0 = filament 1): the preview's palette. */
        palette: string[];
        /** Edits for the 3MF writer at slice time. */
        paintMap: PaintMap;
        /** Spools to send so the daemon has a filament per colour (null when no new colour was added). */
        spools: SpoolOption[] | null;
    }
</script>

<script lang="ts">
    import { untrack } from 'svelte';
    import { textOn } from './colour';
    import type { LoadedModel, PreviewPart } from './model';
    import { PaintSession, paintable } from './paintstate';
    import type { PaintMode } from './paintregion';

    interface Props {
        model: LoadedModel | null;
        /** The file's filament colours (index 0 = filament 1). */
        fileColours: string[];
        /** The daemon can add a filament for a painted new colour; when false, only the file's colours paint. */
        canAddColours?: boolean;
        /** Paint mode on/off (the preview picks on tap while true). */
        active?: boolean;
        onpaint?: (info: PaintInfo) => void;
    }
    let { model, fileColours, canAddColours = true, active = $bindable(false), onpaint }: Props = $props();

    /** Distinct-ish defaults offered by "Add colour" (cycled, then a random hue). */
    const DEFAULTS = ['#E53935', '#1E88E5', '#43A047', '#FDD835', '#8E24AA', '#FB8C00', '#00ACC1', '#6D4C41'];

    let session = $state<PaintSession | null>(null);
    let colours = $state<string[]>([]);
    let activeIndex = $state(0); // 0-based; filament number = activeIndex + 1
    let erase = $state(false);
    let picking = $state(false); // eyedropper: the next tap adopts the tapped colour
    let mirror = $state(false); // also paint the symmetric (left/right) spot
    let mode = $state<PaintMode>('region');
    let crease = $state(30);
    /** Bumped after every edit so derived UI (undo/redo enabled, dirty) re-reads the session. */
    let rev = $state(0);

    const paintablePartCount = $derived(model ? model.parts.filter(paintable).length : 0);
    const canPaint = $derived(paintablePartCount > 0);

    /** Most colours a user can paint with (daemon caps spools at 64; triangle states are bytes). */
    const MAX_COLOURS = 16;

    // A new model: start a fresh session and reset the palette to the file's colours. Read the two inputs as
    // dependencies, then do every state write inside untrack() — otherwise the effect re-reads what it writes
    // (session, colours, rev, and emit()'s reads) and Svelte re-runs it until the depth guard trips.
    $effect(() => {
        const m = model;
        const fc = fileColours;
        untrack(() => {
            if (!m) {
                session = null;
                return;
            }
            session = new PaintSession(m.parts);
            colours = fc.length ? [...fc] : ['#CCCCCC'];
            activeIndex = Math.min(activeIndex, colours.length - 1);
            rev++;
            emit();
        });
    });

    function nextColour(): string {
        const i = colours.length;
        return i < DEFAULTS.length ? DEFAULTS[i] : `#${Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0')}`;
    }

    function addColour(): void {
        if (colours.length >= MAX_COLOURS || !canAddColours) return;
        colours = [...colours, nextColour()];
        activeIndex = colours.length - 1;
        erase = false;
        emit(); // a new colour widens the preview palette even before it is painted
    }

    function setColour(i: number, hex: string): void {
        colours = colours.map((c, j) => (j === i ? hex : c));
        emit();
    }

    /** The highest filament number painted anywhere (0 when nothing is painted). */
    function maxUsed(): number {
        let m = 0;
        for (const f of session?.usedFilaments() ?? []) m = Math.max(m, f);
        return m;
    }

    function emit(): void {
        if (!session) return;
        const used = maxUsed();
        const recoloured = colours.some((c, i) => c !== fileColours[i]); // a new colour, or a changed existing one
        // Send spools when a colour was painted past the file's filaments (the daemon appends those), or when a
        // filament's colour was changed (so the print uses the shown colour). Cover every filament the print
        // uses: the file's filaments plus any appended ones. Re-assigning regions among the file's own,
        // unchanged colours needs none (the paint_color in the 3MF is enough).
        const count = used > fileColours.length ? used : recoloured ? Math.max(used, fileColours.length) : 0;
        const spools: SpoolOption[] | null = count
            ? Array.from({ length: count }, (_, i) => ({ id: null, type: null, colour: colours[i] ?? fileColours[i] ?? '#CCCCCC' }))
            : null;
        onpaint?.({ dirty: session.dirty, palette: colours, paintMap: session.toPaintMap(), spools });
    }

    /** Called by the page when the preview is tapped in paint mode. Returns true when anything changed. */
    export function pick(part: PreviewPart, face: number): boolean {
        if (!session) return false;
        if (picking) {
            // Eyedropper: adopt the tapped triangle's colour (or switch to erase on an unpainted area).
            const st = session.stateAt(part, face);
            if (st > 0 && st <= colours.length) {
                activeIndex = st - 1;
                erase = false;
            } else {
                erase = true;
            }
            picking = false; // one-shot
            return false;
        }
        const changed = session.paint(part, face, mode, erase ? 0 : activeIndex + 1, crease, mirror);
        if (changed) {
            rev++;
            emit();
        }
        return changed;
    }

    function act(fn: () => boolean): void {
        if (fn()) {
            rev++;
            emit();
        }
    }
</script>

{#if canPaint}
    <div class="cp">
        <button
            type="button"
            class="pp-btn wide"
            class:primary={active}
            aria-pressed={active}
            onclick={() => (active = !active)}
        >
            {active ? 'Done painting' : 'Paint colours'}
        </button>

        {#if active}
            <p class="pp-muted pp-small">
                {#if picking}Tap the model to pick up that colour.{:else}Tap the model to paint the {mode === 'face' ? 'triangle' : mode === 'fill' ? 'connected same-colour area' : mode}.{/if}
                {#if canAddColours}Painting a new colour adds a filament to map below.{:else}Update beambam (<code>pip install -U beambam</code>) to paint colours the file doesn't already have.{/if}
            </p>

            <div class="cp-palette" role="group" aria-label="Paint colours">
                {#each colours as c, i (i)}
                    <label class="cp-swatch" class:on={!erase && activeIndex === i} style="background:{c};color:{textOn(c)}">
                        <input
                            type="radio"
                            name="cp-colour"
                            class="sr-only"
                            checked={!erase && activeIndex === i}
                            onchange={() => { activeIndex = i; erase = false; }}
                        />
                        <span>{i + 1}</span>
                        <input
                            type="color"
                            class="cp-pick"
                            value={c}
                            aria-label={`Colour of filament ${i + 1}`}
                            oninput={(e) => setColour(i, (e.currentTarget as HTMLInputElement).value)}
                        />
                    </label>
                {/each}
                {#if canAddColours}
                    <button type="button" class="cp-add" onclick={addColour} disabled={colours.length >= MAX_COLOURS} aria-label="Add a colour">+</button>
                {/if}
                <button
                    type="button"
                    class="cp-swatch cp-erase"
                    class:on={erase}
                    aria-pressed={erase}
                    onclick={() => (erase = true)}
                    title="Erase paint back to the part's own colour"
                >⌫</button>
            </div>

            <div class="cp-row">
                <div class="pp-seg" role="group" aria-label="What a tap paints">
                    {#each [['face', 'Triangle'], ['region', 'Region'], ['part', 'Part'], ['fill', 'Fill']] as [m, label] (m)}
                        <button type="button" class="pp-seg-btn" class:on={mode === m} onclick={() => (mode = m as PaintMode)}>{label}</button>
                    {/each}
                </div>
                <button type="button" class="pp-btn" class:primary={picking} aria-pressed={picking} onclick={() => (picking = !picking)} title="Eyedropper: tap the model to adopt that colour">Pick</button>
                <button type="button" class="pp-btn" class:primary={mirror} aria-pressed={mirror} onclick={() => (mirror = !mirror)} title="Also paint the symmetric (left/right) spot">Mirror</button>
            </div>
            {#if mode === 'region'}
                <label class="cp-crease pp-small">
                    Crease {crease}°
                    <input type="range" min="5" max="90" step="5" bind:value={crease} />
                </label>
            {/if}

            <div class="cp-row">
                {#key rev}
                    <button type="button" class="pp-btn" onclick={() => act(() => session!.grow())} title="Grow the last painted area by one ring">Grow</button>
                    <button type="button" class="pp-btn" onclick={() => act(() => session!.shrink())} title="Shrink the last painted area by one ring">Shrink</button>
                    <button type="button" class="pp-btn" disabled={!session?.canUndo} onclick={() => act(() => session!.undo())}>Undo</button>
                    <button type="button" class="pp-btn" disabled={!session?.canRedo} onclick={() => act(() => session!.redo())}>Redo</button>
                    <button type="button" class="pp-btn" disabled={!session?.dirty} onclick={() => act(() => session!.clear())}>Clear</button>
                {/key}
            </div>
        {/if}
    </div>
{/if}

<style>
    .cp {
        display: grid;
        gap: var(--space-sm);
        margin-top: var(--space-md);
    }
    .cp-palette {
        display: flex;
        flex-wrap: wrap;
        gap: var(--space-xs);
        align-items: center;
    }
    .cp-swatch {
        position: relative;
        width: 44px;
        height: 44px;
        border-radius: var(--radius-sm, 0.4rem);
        border: 2px solid transparent;
        display: grid;
        place-items: center;
        font-size: var(--text-sm);
        font-weight: 700;
        cursor: pointer;
        user-select: none;
    }
    .cp-swatch.on {
        border-color: var(--color-fg, #fff);
        box-shadow: 0 0 0 2px var(--color-accent);
    }
    .cp-pick {
        position: absolute;
        inset: 0;
        opacity: 0;
        width: 100%;
        height: 100%;
        border: 0;
        padding: 0;
        cursor: pointer;
    }
    .cp-add,
    .cp-erase {
        width: 44px;
        height: 44px;
        border-radius: var(--radius-sm, 0.4rem);
        border: 1px dashed var(--color-line, #666);
        background: transparent;
        color: var(--color-mute);
        font-size: 1.25rem;
        cursor: pointer;
    }
    .cp-erase {
        border-style: solid;
        background: var(--color-surface, #222);
    }
    .cp-row {
        display: flex;
        flex-wrap: wrap;
        gap: var(--space-xs);
        align-items: center;
    }
    .pp-seg {
        display: inline-flex;
        border: 1px solid var(--color-line, #555);
        border-radius: var(--radius-sm, 0.4rem);
        overflow: hidden;
    }
    .pp-seg-btn {
        appearance: none;
        background: transparent;
        color: var(--color-mute);
        border: 0;
        padding: var(--space-xs) var(--space-md);
        min-height: 40px;
        cursor: pointer;
    }
    .pp-seg-btn.on {
        background: var(--color-accent);
        color: var(--color-on-accent, #111);
    }
    .cp-crease {
        display: flex;
        gap: var(--space-sm);
        align-items: center;
    }
    .cp-crease input {
        flex: 1;
        accent-color: var(--color-accent);
    }
    .sr-only {
        position: absolute;
        width: 1px;
        height: 1px;
        padding: 0;
        margin: -1px;
        overflow: hidden;
        clip: rect(0, 0, 0, 0);
        white-space: nowrap;
        border: 0;
    }
</style>
