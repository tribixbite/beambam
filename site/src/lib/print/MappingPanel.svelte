<script lang="ts">
    /**
     * Filament -> spool mapping for one print file, kept in step with the printer.
     *
     * One row per filament from beambam's plan (POST /print/plan): its colour and material,
     * the spool it prints from (a picker of the spools loaded now, closest colour first with
     * its ΔE), and beambam's verdict: errors (✗) block Start, warnings (⚠) are accepted by
     * pressing it. The page re-reads the spools every few seconds; when they change (a spool
     * swapped or run out) the plan is checked again, so what is on screen is what Start sends.
     *
     * On a two-nozzle printer with a daemon that reports `plan-sliced-nozzles`, each filament
     * shows a Left | Right switch: picking the other nozzle is a different print (tool order,
     * purge, time), so the page slices again with the `nozzles` option and hands this panel
     * the new file. "Let Bambu choose" slices again without it.
     */
    import { normHex } from './colour';
    import type { Daemon } from './daemon';
    import {
        isTwoNozzlePlan,
        nozzleNumber,
        nozzleMismatches,
        nozzlesWith,
        slotsFromPlan,
        spoolChoices,
        withSlot,
        type PlanFilament,
        type PrintPlan
    } from './plan';
    import { shortLabel, type Tray } from './printer';
    import SpoolPicker from './SpoolPicker.svelte';
    import Swatch from './Swatch.svelte';

    interface Props {
        daemon: Daemon;
        /** The print file's path on the daemon's machine. */
        file: string;
        trays: Tray[];
        /** Changes whenever the loaded spools change (printer.ts `traySignature`). */
        traySig: string;
        /** Slicing again for other nozzles is possible (project file + capable daemon). */
        canRenozzle: boolean;
        /** The nozzles the user chose (null: Bambu chose). */
        nozzles: number[] | null;
        /** A re-slice is running (rows are frozen meanwhile). */
        reslicing: boolean;
        /** grams per filament of the current file (index 0 = filament 1), when known. */
        grams: number[];
        /** Trays to plan with first (a mesh file: the spool picked for it); null = beambam's choice. */
        initialSlots: number[] | null;
        /** A print is being started or runs: no more changes. */
        locked: boolean;
        onrenozzle: (nozzles: number[] | null, what: string) => void;
        /** The current plan and the trays Start would send (null: cannot start). */
        onchange: (plan: PrintPlan | null, slots: number[] | null) => void;
    }
    let { daemon, file, trays, traySig, canRenozzle, nozzles, reslicing, grams, initialSlots, locked, onrenozzle, onchange }: Props =
        $props();

    let plan = $state<PrintPlan | null>(null);
    let slots = $state<number[] | null>(null);
    let error = $state('');
    let planning = $state(false);
    let sync = $state('');
    let pickerOpen = $state(0);
    let pendingReason = '';
    let seen = '';
    let lastFile = '';
    let generation = 0;
    let chain: Promise<void> = Promise.resolve();

    const clock = (): string => new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    /** Ask beambam for the plan with the current slots; later calls queue behind earlier ones. */
    function replan(reason: string): Promise<void> {
        const gen = ++generation;
        const forFile = file;
        const run = async (): Promise<void> => {
            if (gen !== generation) return; // a newer request supersedes this one
            planning = true;
            try {
                const raw = await daemon.plan(forFile, slots);
                if (gen !== generation || forFile !== file) return;
                const p = raw as unknown as PrintPlan;
                p.filaments = Array.isArray(p.filaments) ? p.filaments : [];
                p.errors = Array.isArray(p.errors) ? p.errors : [];
                p.warnings = Array.isArray(p.warnings) ? p.warnings : [];
                p.notes = Array.isArray(p.notes) ? p.notes : [];
                if (!p.filaments.length && typeof (raw as { error?: unknown }).error === 'string') {
                    p.errors = [String((raw as { error: string }).error)];
                }
                plan = p;
                slots = slotsFromPlan(p) ?? slots;
                error = '';
                if (reason) sync = reason;
                onchange(p, slotsFromPlan(p));
            } catch (e) {
                if (gen !== generation) return;
                plan = null;
                error = e instanceof Error ? e.message : String(e);
                onchange(null, null);
            } finally {
                if (gen === generation) planning = false;
            }
        };
        chain = chain.then(run, run);
        return chain;
    }

    // A new print file: start from the given trays, else beambam's own choice. A re-slice of
    // the same model keeps the spools the user picked (same filaments, same order).
    $effect(() => {
        if (file && file !== lastFile) {
            const resliced = lastFile !== '' && plan !== null;
            lastFile = file;
            slots = resliced ? slots : initialSlots;
            plan = null;
            void replan('');
        }
    });

    // The spools changed: check the plan again (after an open spool list closes).
    $effect(() => {
        const sig = traySig;
        if (!seen) {
            seen = sig;
            return;
        }
        if (sig === seen) return;
        seen = sig;
        const reason = `The printer's spools changed at ${clock()}: checked again.`;
        if (pickerOpen > 0 || reslicing || locked) pendingReason = reason;
        else void replan(reason);
    });

    $effect(() => {
        if (pickerOpen === 0 && pendingReason && !reslicing && !locked) {
            const r = pendingReason;
            pendingReason = '';
            void replan(r);
        }
    });

    function pick(index: number, trayId: number): void {
        if (!plan || trayId < 0) return;
        slots = withSlot(plan, slots, index, trayId);
        void replan('');
    }

    function verdict(f: PlanFilament): { mark: string; cls: string } {
        if (f.errors.length) return { mark: '✗', cls: 'pp-err-text' };
        if (f.warnings.length) return { mark: '⚠', cls: 'pp-warn-text' };
        return { mark: '✓', cls: 'pp-ok-text' };
    }

    const twoNozzles = $derived(plan ? isTwoNozzlePlan(plan) : false);
    const frozen = $derived(reslicing || locked);
</script>

<div class="mp">
    {#if error}
        <div class="pp-msg err" role="alert">{error}</div>
        <button type="button" class="pp-btn" onclick={() => replan('')}>Check again</button>
    {:else if !plan}
        <div class="pp-muted" aria-live="polite">Checking the spools with beambam…</div>
    {:else}
        {#if twoNozzles && canRenozzle}
            <div class="mp-nozzles pp-msg info">
                {#if nozzles}
                    {@const off = nozzleMismatches(plan, nozzles)}
                    {#if off.length}
                        <span class="pp-warn-text">Bambu did not slice every filament on the nozzle you picked: {off.join('; ')}. The rows show where each really prints.</span>
                    {:else}
                        <span>Nozzles: your choice (the time and weight above are for it).</span>
                    {/if}
                    <button type="button" class="pp-btn" disabled={frozen} onclick={() => onrenozzle(null, "Bambu's choice of nozzles")}>
                        Let Bambu choose
                    </button>
                {:else}
                    <span>Nozzles: Bambu's choice (fewest filament changes). Pick Left or Right under a filament to slice it that way.</span>
                {/if}
            </div>
        {:else if twoNozzles}
            <div class="pp-msg info pp-small">
                Each filament's nozzle was chosen when this file was sliced (shown under it).
            </div>
        {/if}

        <ol class="mp-rows" aria-label="Filaments and the spools they print from">
            {#each plan.filaments as f, i (f.filament)}
                {@const v = verdict(f)}
                {@const colour = normHex(f.colour)}
                {@const current = f.tray?.tray_id ?? null}
                {@const nz = nozzleNumber(f.nozzle)}
                <li class="mp-row" class:bad={f.errors.length > 0} class:warn={!f.errors.length && f.warnings.length > 0}>
                    <div class="mp-fil">
                        <Swatch colour={colour} text={String(f.filament)} size="lg" title={`Filament ${f.filament}: ${colour}`} />
                        <div class="mp-what">
                            <div class="pp-mono">{f.type || 'Filament'} <span class="pp-muted">{colour}</span></div>
                            <div class="pp-muted pp-small">
                                filament {f.filament}{grams[f.filament - 1] ? ` · ${grams[f.filament - 1].toFixed(1)} g` : ''}{nz && !canRenozzle ? ` · ${f.nozzle} nozzle` : ''}
                                {#if f.tray && !trays.some((t) => t.trayId === f.tray?.tray_id)} · was {shortLabel(f.tray.label)}{/if}
                            </div>
                        </div>
                        <span class="mp-verdict {v.cls}" aria-label={v.mark === '✓' ? 'matches' : v.mark === '✗' ? 'error' : 'warning'}>{v.mark}</span>
                    </div>
                    <SpoolPicker
                        choices={spoolChoices(trays, colour, current, f.type)}
                        value={current}
                        label={`Filament ${f.filament} (${f.type})`}
                        disabled={frozen}
                        onpick={(t) => pick(i, t)}
                        onopenchange={(o) => (pickerOpen = Math.max(0, pickerOpen + (o ? 1 : -1)))}
                    />
                    {#if canRenozzle && nz !== null}
                        <div class="mp-nozzle">
                            <span class="pp-legend">Nozzle</span>
                            <div class="pp-seg" role="radiogroup" aria-label={`Nozzle for filament ${f.filament}`}>
                                {#each [[1, 'Left'], [2, 'Right']] as const as [n, name] (n)}
                                    <button
                                        type="button"
                                        role="radio"
                                        aria-checked={n === nz}
                                        disabled={frozen}
                                        onclick={() => {
                                            if (n !== nz && plan) onrenozzle(nozzlesWith(plan, f.filament, n), `filament ${f.filament} on the ${name.toLowerCase()} nozzle`);
                                        }}
                                    >{name}</button>
                                {/each}
                            </div>
                        </div>
                    {/if}
                    {#each f.errors as e}<div class="pp-err-text pp-small">✗ {e}</div>{/each}
                    {#each f.warnings as w}<div class="pp-warn-text pp-small">⚠ {w}</div>{/each}
                </li>
            {/each}
        </ol>

        <div class="mp-check" aria-live="polite">
            {#if plan.errors.length}
                <div class="pp-msg err">
                    beambam will not start this print: fix the ✗ rows (pick another spool, or load one).
                    {#each plan.errors as e}<div class="pp-small">✗ {e}</div>{/each}
                </div>
            {:else if plan.warnings.length}
                <div class="pp-msg warn">
                    Spool check: Start print accepts the ⚠ differences.
                    {#each plan.warnings as w}<div class="pp-small">⚠ {w}</div>{/each}
                </div>
            {:else}
                <div class="pp-msg ok">Spool check: every spool matches.</div>
            {/if}
            {#each plan.notes as n}<div class="pp-muted pp-small">{n}</div>{/each}
        </div>
    {/if}
    <div class="pp-muted pp-small mp-sync" aria-live="polite">
        {planning ? 'Checking with beambam…' : sync || 'Spools are re-read from the printer every 4 s; a change is checked again here.'}
    </div>
</div>

<style>
    .mp { display: grid; gap: var(--space-md); }
    .mp-nozzles { display: flex; flex-wrap: wrap; gap: var(--space-sm); align-items: center; justify-content: space-between; }
    .mp-rows { list-style: none; padding: 0; margin: 0; display: grid; gap: var(--space-md); }
    .mp-row {
        display: grid;
        gap: var(--space-sm);
        padding: var(--space-md);
        border: 1px solid var(--color-hair);
        border-left: 3px solid oklch(70% 0.15 150);
        background: color-mix(in oklab, var(--color-surface-2) 70%, transparent);
    }
    .mp-row.warn { border-left-color: var(--color-accent); }
    .mp-row.bad { border-left-color: oklch(65% 0.2 25); }
    .mp-fil { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: var(--space-md); }
    .mp-what { min-width: 0; overflow-wrap: anywhere; }
    .mp-verdict { font-size: 1.5rem; line-height: 1; min-width: 1.5rem; text-align: center; }
    .mp-nozzle { display: grid; grid-template-columns: auto 1fr; align-items: center; gap: var(--space-md); }
    .mp-sync { min-height: 1.2em; }
</style>
