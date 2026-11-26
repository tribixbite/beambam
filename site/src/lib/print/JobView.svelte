<script lang="ts">
    /**
     * The printer's current job, live from GET /state (the page re-reads it every few
     * seconds): state, progress, layers, time left and temperatures, with Pause / Resume /
     * Stop. The controls appear for a print started from this page; for a job started
     * elsewhere (Bambu Handy, the touchscreen) they stay hidden until "Control this print" is
     * tapped, so looking at the page never puts a Stop button under a stray finger.
     */
    import type { Daemon } from './daemon';
    import { confirmFocus } from './focus';
    import { isBusy, minutesText, stateText, type PrinterStatus } from './printer';

    interface Props {
        status: PrinterStatus;
        daemon: Daemon;
        /** The job was started from this page. */
        startedHere: boolean;
        /** When the status was read (ms since epoch). */
        readAt: number;
    }
    let { status, daemon, startedHere, readAt }: Props = $props();

    let unlocked = $state(false);
    let confirmStop = $state(false);
    let sending = $state<'' | 'pause' | 'resume' | 'stop'>('');
    let said = $state<{ ok: boolean; text: string } | null>(null);

    const showControls = $derived(startedHere || unlocked);
    const active = $derived(isBusy(status.state));
    const finishAt = $derived(
        status.remainingMin > 0 && active
            ? new Date(readAt + status.remainingMin * 60_000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
            : ''
    );

    async function send(verb: 'pause' | 'resume' | 'stop'): Promise<void> {
        sending = verb;
        said = null;
        try {
            said = await daemon.control(verb);
        } catch (e) {
            said = { ok: false, text: e instanceof Error ? e.message : String(e) };
        } finally {
            sending = '';
            confirmStop = false;
        }
    }

    const temp = (cur: number, target: number): string =>
        `${Math.round(cur)}°${target > 0 ? ` → ${Math.round(target)}°` : ''}`;
</script>

<div class="pp-card job">
    <div class="job-head">
        <span class="job-state" aria-live="polite" class:running={status.state === 'RUNNING'} class:paused={status.state === 'PAUSE'}>
            {stateText(status.state)}
        </span>
        {#if status.job}<span class="job-name">{status.job}</span>{/if}
    </div>

    <!-- No aria-live here: it announced every percent. The progressbar reads out on demand, and the state
         line above announces the changes that matter (paused, failed, finished). -->
    <div class="job-progress">
        <div class="job-pct pp-mono">{status.percent}<span>%</span></div>
        <div class="pp-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={status.percent}
             aria-label="Print progress">
            <div style:width={`${status.percent}%`}></div>
        </div>
    </div>

    <dl class="pp-kv">
        <dt>Layer</dt>
        <dd>{status.totalLayers ? `${status.layer} / ${status.totalLayers}` : '—'}</dd>
        <dt>Time left</dt>
        <dd>{active && status.remainingMin ? `${minutesText(status.remainingMin)} (done ≈ ${finishAt})` : '—'}</dd>
        {#each status.nozzles as n (n.name)}
            <dt>{status.dual ? `${n.name} nozzle` : 'Nozzle'}</dt>
            <dd>{temp(n.current, n.target)}</dd>
        {/each}
        <dt>Bed</dt>
        <dd>{temp(status.bed, status.bedTarget)}</dd>
    </dl>

    {#if active}
        {#if showControls}
            <div class="job-controls">
                {#if status.state === 'PAUSE'}
                    <button type="button" class="pp-btn primary" disabled={!!sending} onclick={() => send('resume')}>
                        {sending === 'resume' ? 'Resuming…' : 'Resume'}
                    </button>
                {:else}
                    <button type="button" class="pp-btn" disabled={!!sending} onclick={() => send('pause')}>
                        {sending === 'pause' ? 'Pausing…' : 'Pause'}
                    </button>
                {/if}
                {#if confirmStop}
                    <div class="pp-msg err job-confirm" role="alertdialog" aria-label="Stop this print?"
                         use:confirmFocus={{ initial: '.keep', returnTo: '.stop-open', onEscape: () => (confirmStop = false) }}>
                        <div>Stop this print? The printer cancels the job; it cannot be resumed.</div>
                        <div class="pp-row" style="margin-top: var(--space-sm);">
                            <button type="button" class="pp-btn danger solid" disabled={!!sending} onclick={() => send('stop')}>
                                {sending === 'stop' ? 'Stopping…' : 'Yes, stop it'}
                            </button>
                            <button type="button" class="pp-btn keep" onclick={() => (confirmStop = false)}>Keep printing</button>
                        </div>
                    </div>
                {:else}
                    <button type="button" class="pp-btn danger stop-open" disabled={!!sending} onclick={() => (confirmStop = true)}>Stop…</button>
                {/if}
            </div>
        {:else}
            <button type="button" class="pp-btn" onclick={() => (unlocked = true)}>Control this print</button>
            <p class="pp-muted pp-small">This job was started elsewhere; its Pause / Resume / Stop are one tap away.</p>
        {/if}
    {/if}
    {#if said}
        <div class="pp-msg {said.ok ? 'ok' : 'err'}" role="status">{said.text}</div>
    {/if}
</div>

<style>
    .job { display: grid; gap: var(--space-lg); }
    .job-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: var(--space-sm) var(--space-md); }
    .job-state {
        font-family: var(--font-mono);
        font-size: var(--text-xs);
        letter-spacing: 0.15em;
        text-transform: uppercase;
        border: 1px solid var(--color-hair);
        padding: 2px 8px;
        color: var(--color-mute);
    }
    .job-state.running { color: var(--color-accent); border-color: var(--color-accent); }
    .job-state.paused { color: oklch(80% 0.12 90); border-color: currentColor; }
    .job-name { font-family: var(--font-display); font-size: var(--text-lg); text-transform: uppercase; line-height: 1.1; overflow-wrap: anywhere; }
    .job-progress { display: grid; gap: var(--space-sm); }
    .job-pct { font-size: 2.5rem; line-height: 1; }
    .job-pct span { font-size: var(--text-base); color: var(--color-mute); margin-left: 2px; }
    .job-controls { display: flex; flex-wrap: wrap; gap: var(--space-sm); align-items: flex-start; }
    .job-confirm { width: 100%; margin: 0; }
</style>
