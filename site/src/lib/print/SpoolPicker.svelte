<script lang="ts">
    /**
     * Pick the spool a filament prints from: a button showing the chosen spool (swatch, tray,
     * material, ΔE to the filament colour) that expands, in place, into the list of loaded
     * spools, closest colour first. Expanding in the flow (not a floating popup) keeps it
     * usable on a phone: nothing to position, and the page scrolls with it.
     *
     * The list is a snapshot taken when it opens, so a spool re-read every few seconds never
     * moves an option under the user's finger. Escape closes it; arrow keys move between options.
     */
    import { deltaText, matchLevel } from './colour';
    import type { SpoolChoice } from './plan';
    import Swatch from './Swatch.svelte';

    interface Props {
        choices: SpoolChoice[];
        /** Tray id chosen now, or null for none. */
        value: number | null;
        /** Filament colour the choices are compared with (for the button's own badge). */
        label: string;
        disabled?: boolean;
        /** Show the ΔE badge (off when there is no filament colour to compare with). */
        showMatch?: boolean;
        onpick: (trayId: number) => void;
        /** Tells the page a list is open (it holds back re-planning meanwhile). */
        onopenchange?: (open: boolean) => void;
    }
    let { choices, value, label, disabled = false, showMatch = true, onpick, onopenchange }: Props = $props();

    let open = $state(false);
    let snapshot = $state<SpoolChoice[]>([]);
    let listEl: HTMLDivElement | undefined = $state();
    let buttonEl: HTMLButtonElement | undefined = $state();
    const listId = `spools-${Math.random().toString(36).slice(2, 9)}`;

    const current = $derived(choices.find((c) => c.trayId === value) ?? null);

    function badge(c: SpoolChoice | null): { text: string; cls: string } {
        if (!c) return { text: 'pick one', cls: 'none' };
        if (c.unavailable || !Number.isFinite(c.de)) return { text: c.unavailable ? 'not loaded' : '—', cls: 'none' };
        return { text: deltaText(c.de), cls: matchLevel(c.de) };
    }

    const currentBadge = $derived(badge(current));

    function setOpen(v: boolean): void {
        open = v;
        if (v) snapshot = choices;
        onopenchange?.(v);
        if (v) queueMicrotask(() => (listEl?.querySelector('[aria-pressed="true"]') as HTMLElement | null ?? listEl?.querySelector('button'))?.focus());
    }

    function pick(c: SpoolChoice): void {
        if (c.unavailable) return;
        setOpen(false);
        buttonEl?.focus();
        if (c.trayId !== value) onpick(c.trayId);
    }

    function onKey(e: KeyboardEvent): void {
        if (!listEl) return;
        const items = [...listEl.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
        const i = items.indexOf(document.activeElement as HTMLButtonElement);
        if (e.key === 'Escape') {
            setOpen(false);
            buttonEl?.focus();
        } else if (e.key === 'ArrowDown') items[(i + 1) % items.length]?.focus();
        else if (e.key === 'ArrowUp') items[(i - 1 + items.length) % items.length]?.focus();
        else if (e.key === 'Home') items[0]?.focus();
        else if (e.key === 'End') items[items.length - 1]?.focus();
        else return;
        e.preventDefault();
    }
</script>

<div class="sp">
    <button
        type="button"
        class="sp-btn"
        bind:this={buttonEl}
        aria-expanded={open}
        aria-controls={listId}
        aria-label={`${label}: ${current ? `${current.label}, ${current.detail}` : 'no spool chosen'}. Change spool`}
        {disabled}
        onclick={() => setOpen(!open)}
    >
        <Swatch colour={current?.colour ?? ''} />
        <span class="sp-main">
            <span class="sp-label">{current?.label ?? 'No spool'}</span>
            <span class="sp-detail">{current?.detail ?? 'pick the spool to print it from'}</span>
        </span>
        {#if showMatch}<span class="pp-de {currentBadge.cls}">{currentBadge.text}</span>{/if}
        <span class="sp-caret" aria-hidden="true">{open ? '▴' : '▾'}</span>
    </button>

    {#if open}
        <!-- Arrow keys move between the option buttons (each is itself focusable and clickable). -->
        <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
        <div class="sp-list" id={listId} bind:this={listEl} onkeydown={onKey} role="group" aria-label={`Spools for ${label}`}>
            {#each snapshot as c, i (c.trayId)}
                {@const b = badge(c)}
                <button
                    type="button"
                    class="sp-opt"
                    aria-pressed={c.trayId === value}
                    disabled={c.unavailable}
                    onclick={() => pick(c)}
                >
                    <Swatch colour={c.colour} />
                    <span class="sp-main">
                        <span class="sp-label">
                            {c.label}
                            {#if showMatch && i === 0 && Number.isFinite(c.de)}<em>closest</em>{/if}
                        </span>
                        <span class="sp-detail">{c.detail}</span>
                    </span>
                    {#if showMatch}<span class="pp-de {b.cls}">{b.text}</span>{:else}<span></span>{/if}
                </button>
            {:else}
                <div class="pp-muted" style="padding: var(--space-md);">No spools are loaded on the printer.</div>
            {/each}
        </div>
    {/if}
</div>

<style>
    .sp { display: grid; }
    .sp-btn,
    .sp-opt {
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) auto auto;
        align-items: center;
        gap: var(--space-md);
        min-height: 52px;
        width: 100%;
        padding: var(--space-sm) var(--space-md);
        background: var(--color-surface-3);
        border: 1px solid var(--color-hair);
        color: var(--color-ink);
        text-align: left;
        cursor: pointer;
        font: inherit;
    }
    .sp-opt { grid-template-columns: auto minmax(0, 1fr) auto; border-top: 0; background: var(--color-surface-2); }
    .sp-btn:hover:not(:disabled),
    .sp-opt:hover:not(:disabled) { border-color: var(--color-accent); }
    .sp-btn:focus-visible,
    .sp-opt:focus-visible { outline: 2px solid var(--color-accent); outline-offset: -2px; }
    .sp-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .sp-opt:disabled { cursor: not-allowed; opacity: 0.55; }
    .sp-opt[aria-pressed='true'] { box-shadow: inset 3px 0 0 var(--color-accent); }
    .sp-main { display: grid; min-width: 0; }
    .sp-label { font-family: var(--font-mono); font-size: var(--text-sm); }
    .sp-label em { font-style: normal; color: var(--color-accent); font-size: 11px; margin-left: 0.4em; letter-spacing: 0.08em; text-transform: uppercase; }
    .sp-detail { font-size: var(--text-xs); color: var(--color-mute); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .sp-caret { color: var(--color-mute); font-size: var(--text-xs); }
    .sp-list { max-height: 22rem; overflow-y: auto; overscroll-behavior: contain; border-bottom: 1px solid var(--color-hair); }
</style>
