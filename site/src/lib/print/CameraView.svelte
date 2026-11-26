<script lang="ts">
    /**
     * The chamber camera, live through beambam (`GET /camera.mjpeg`, which relays the camera
     * proxy's MJPEG). Off until asked for (the stream is ~1.5 MB/s), remembered per browser;
     * the stream is dropped while the tab is hidden and picked up again when it is shown. When
     * it fails the card says why in beambam's words (`Daemon.cameraProblem`).
     */
    import { onDestroy, onMount } from 'svelte';
    import type { Daemon } from './daemon';

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
    /** Bumped for every (re)connect: a new URL restarts the stream. */
    let attempt = $state(1);
    let showing = $state(false);
    let problem = $state('');
    let checking = $state(false);
    let retried = false;

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
        showing = false;
        retried = false;
        attempt += 1;
    }

    async function failed(e: Event): Promise<void> {
        if (e.currentTarget && torn.has(e.currentTarget)) return;
        showing = false;
        checking = true;
        const why = await daemon.cameraProblem();
        checking = false;
        if (why === null && !retried) {
            // A frame comes through now: the stream dropped (proxy restart, network blip). One quiet retry.
            retried = true;
            attempt += 1;
            return;
        }
        problem = why ?? 'The live view stopped.';
    }

    /**
     * Close the MJPEG connection when the image goes (hidden, toggled off, a retry): Chrome keeps a removed
     * multipart <img> streaming unless its src is cleared first (seen: 14 s and counting, a second stream on
     * the next show).
     */
    const torn = new WeakSet<EventTarget>();
    function stream(node: HTMLImageElement): { destroy: () => void } {
        return {
            destroy: () => {
                torn.add(node);             // its error event from the cleared src is not a camera failure
                node.removeAttribute('src');
            }
        };
    }

    const streaming = $derived(on && !hidden && available !== false && available !== null && !problem);
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
        <div class="cam-frame" class:ready={showing}>
            {#if streaming}
                {#key attempt}
                    <!-- crossorigin: the request carries this page's Origin, which beambam checks (another site's
                         page cannot start the camera). -->
                    <img use:stream src={daemon.cameraUrl(attempt)} alt="Live view of the printer's chamber" crossorigin="anonymous"
                         onload={() => (showing = true)} onerror={failed} />
                {/key}
            {/if}
            {#if !showing}
                <span class="cam-wait pp-mono pp-small" aria-live="polite">{checking ? 'Checking the camera…' : hidden ? 'Paused while this tab is hidden' : 'Connecting to the camera…'}</span>
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
    .cam-frame:not(.ready) img { position: absolute; inset: 0; opacity: 0; }
    .cam-wait { color: var(--color-mute); padding: var(--space-md); text-align: center; }
    .pp-link {
        background: none; border: 0; padding: 0; color: var(--color-accent); font: inherit;
        text-decoration: underline; cursor: pointer; min-height: 44px; padding-inline: var(--space-xs);
    }
</style>
