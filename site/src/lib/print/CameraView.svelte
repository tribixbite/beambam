<script lang="ts">
    /**
     * The chamber camera, live through beambam (`GET /camera.mjpeg`, which relays the camera
     * proxy's MJPEG), read with fetch (`readMjpeg`) so it carries the daemon's access token and
     * Local Network Access like every other call. Off until asked for (the stream is ~1.5 MB/s),
     * remembered per browser; the stream is dropped while the tab is hidden and picked up again
     * when it is shown. When it fails the card says why in beambam's words.
     */
    import { onDestroy, onMount } from 'svelte';
    import type { Daemon } from './daemon';
    import { newestFrame, readMjpeg } from './mjpeg';

    interface Props {
        daemon: Daemon;
        /** /healthz `camera`: the proxy is up (true), down (false), or this beambam has no relay (null). */
        available: boolean | null;
        /** Read /healthz again (the proxy may have been started since). */
        recheck?: () => void | Promise<void>;
    }
    let { daemon, available, recheck }: Props = $props();

    const STORE_KEY = 'beambam.print.camera';
    let on = $state(false);
    let hidden = $state(false);
    /** Bumped for every (re)connect: the stream effect restarts. */
    let attempt = $state(1);
    /** The frame on screen (an object URL from `newestFrame`), '' before the first and once stopped. */
    let frame = $state('');
    let problem = $state('');

    onMount(() => {
        try {
            on = localStorage.getItem(STORE_KEY) === '1';
        } catch {
            /* storage blocked: off */
        }
        hidden = document.visibilityState === 'hidden';
        document.addEventListener('visibilitychange', onVisibility);
    });
    onDestroy(() => {
        if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
    });

    function onVisibility(): void {
        hidden = document.visibilityState === 'hidden';
        if (!hidden) reconnect();
    }

    function toggle(): void {
        on = !on;
        try {
            localStorage.setItem(STORE_KEY, on ? '1' : '0');
        } catch {
            /* storage blocked: this visit only */
        }
        if (on) reconnect();
    }

    function reconnect(): void {
        void recheck?.();
        problem = '';
        attempt += 1;
    }

    const streaming = $derived(on && !hidden && available === true && !problem);

    // One stream per (re)connect while it should show; its teardown aborts the fetch, which closes the
    // daemon's relay at once (an <img> kept streaming after removal unless its src was cleared).
    $effect(() => {
        if (!streaming) return;
        void attempt;
        const d = daemon;
        const ctl = new AbortController();
        const onFrame = newestFrame((url) => (frame = url), ctl.signal);
        (async () => {
            let dropped = 0;
            while (!ctl.signal.aborted) {
                try {
                    const res = await d.camera(ctl.signal);
                    await readMjpeg(res, (jpeg) => {
                        dropped = 0;
                        onFrame(jpeg);
                    });
                    // The stream ended (the proxy restarted): one quiet reconnect, then say so.
                    if (++dropped > 1) throw new Error('The live view stopped.');
                } catch (e) {
                    if (ctl.signal.aborted) return;
                    problem = e instanceof Error ? e.message : String(e);
                    return;
                }
            }
        })();
        return () => ctl.abort();       // also takes the frame off screen (newestFrame)
    });
</script>

<div class="cam">
    <div class="cam-head">
        <span class="pp-legend">Camera</span>
        {#if available !== null}
            <button type="button" class="pp-btn" aria-pressed={on} onclick={toggle}>{on ? 'Hide live view' : 'Show live view'}</button>
        {/if}
    </div>
    {#if available === null}
        <p class="pp-muted pp-small">This beambam cannot relay the camera yet. Update it (<code>pip install -U beambam</code>) and restart the daemon with <code>--camera</code>.</p>
    {:else if !on}
        <!-- nothing: the stream is off until asked for -->
    {:else if available === false}
        <div class="pp-msg warn">
            The camera proxy is not running. Start it with <code>beambam cam start</code>, or restart the daemon with
            <code>--camera</code>; then <button type="button" class="pp-link" onclick={reconnect}>try again</button>.
        </div>
    {:else if problem}
        <div class="pp-msg err" role="alert">
            {problem} <button type="button" class="pp-link" onclick={reconnect}>Try again</button>
        </div>
    {:else}
        <div class="cam-frame" class:ready={!!frame}>
            {#if frame}
                <img src={frame} alt="Live view of the printer's chamber" />
            {:else}
                <span class="cam-wait pp-mono pp-small" aria-live="polite">{hidden ? 'Paused while this tab is hidden' : 'Connecting to the camera…'}</span>
            {/if}
        </div>
    {/if}
</div>

<style>
    .cam { display: grid; gap: var(--space-sm); min-width: 0; }
    .cam-head { display: flex; align-items: center; justify-content: space-between; gap: var(--space-md); }
    .cam-frame {
        position: relative;
        aspect-ratio: 16 / 9;
        background: #000;
        border: 1px solid var(--color-hair);
        display: grid;
        place-items: center;
        overflow: hidden;
    }
    .cam-frame img { width: 100%; height: 100%; object-fit: contain; display: block; }
    .cam-wait { color: var(--color-mute); padding: var(--space-md); text-align: center; }
    .pp-link {
        background: none; border: 0; padding: 0; color: var(--color-accent); font: inherit;
        text-decoration: underline; cursor: pointer; min-height: 44px; padding-inline: var(--space-xs);
    }
</style>
