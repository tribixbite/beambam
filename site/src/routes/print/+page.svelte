<script lang="ts">
    /**
     * /print — print your own file on your Bambu printer through the beambam daemon on this
     * device or LAN: connect, drop a file, preview it in 3D, choose settings, slice, map each
     * filament to a loaded spool (re-checked live), confirm, and watch the job.
     *
     * Everything runs in this browser against the daemon's HTTP API; nothing is uploaded to
     * beambam.boo. The daemon must be started with this site's origin (--cors-origin).
     */
    import { onDestroy, onMount } from 'svelte';
    import CodeBlock from '$lib/CodeBlock.svelte';
    import '$lib/print/print.css';
    import { Daemon, DaemonError, DEFAULT_URL, SITE_ORIGIN, startCommand, type Health, type SliceResult } from '$lib/print/daemon';
    import JobView from '$lib/print/JobView.svelte';
    import MappingPanel from '$lib/print/MappingPanel.svelte';
    import { fileType, type FileType } from '$lib/print/load';
    import { NEUTRAL, type LoadedModel } from '$lib/print/model';
    import {
        BRIM_MODES_UI,
        buildSliceOptions,
        hasSettings,
        INFILL_STEPS,
        LAYER_HEIGHTS,
        optionsHeader,
        PROJECT_SETTINGS,
        spoolChoices,
        SUPPORT_MODES_UI,
        WALL_COUNTS,
        type PrintPlan,
        type PrintSettings
    } from '$lib/print/plan';
    import {
        isBusy,
        loadedTrays,
        readStatus,
        readTrays,
        gramsText,
        secondsText,
        stateText,
        traySignature,
        type PrinterStatus,
        type Tray
    } from '$lib/print/printer';
    import Preview from '$lib/print/Preview.svelte';
    import SpoolPicker from '$lib/print/SpoolPicker.svelte';
    import Swatch from '$lib/print/Swatch.svelte';

    const STORE_KEY = 'beambam.print.daemon';
    const POLL_MS = 4000;
    const POLL_JOB_MS = 2500;
    const MAX_UPLOAD = 200 * 1024 * 1024;

    // ---------------------------------------------------------------- connection
    let urlInput = $state(DEFAULT_URL);
    let origin = $state(SITE_ORIGIN);
    let daemon = $state<Daemon | null>(null);
    let health = $state<Health | null>(null);
    let connecting = $state(false);
    let connError = $state<DaemonError | null>(null);
    let trays = $state<Tray[]>([]);
    let traySig = $state('');
    let status = $state<PrinterStatus | null>(null);
    let readAt = $state(0);
    let stateError = $state('');
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let destroyed = false;

    const caps = $derived(health?.capabilities ?? []);
    const hasOptions = $derived(caps.includes('slice-options'));
    const hasMesh = $derived(caps.includes('slice-mesh'));
    const hasNozzlePlan = $derived(caps.includes('plan-sliced-nozzles'));
    const loaded = $derived(loadedTrays(trays));
    const command = $derived(startCommand(origin, safeUrl(urlInput)));

    function safeUrl(u: string): string {
        try {
            return new Daemon(u).url;
        } catch {
            return DEFAULT_URL;
        }
    }

    function remember(url: string, auto: boolean): void {
        try {
            localStorage.setItem(STORE_KEY, JSON.stringify({ url, auto }));
        } catch {
            /* storage blocked: remembered for this visit only */
        }
    }

    async function connect(): Promise<void> {
        connError = null;
        connecting = true;
        let d: Daemon;
        try {
            d = new Daemon(urlInput);
        } catch (e) {
            connError = new DaemonError(e instanceof Error ? e.message : String(e), 'bad-request');
            connecting = false;
            return;
        }
        try {
            const h = await d.health();
            daemon = d;
            health = h;
            urlInput = d.url;
            remember(d.url, true);
            await readPrinter();
        } catch (e) {
            daemon = null;
            health = null;
            connError = e instanceof DaemonError ? e : new DaemonError(String(e), 'http');
        } finally {
            connecting = false;
        }
        schedulePoll();
    }

    function disconnect(): void {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
        daemon = null;
        health = null;
        status = null;
        trays = [];
        traySig = '';
        remember(urlInput, false);
    }

    /** Read the printer's report: spools and status. */
    async function readPrinter(): Promise<void> {
        if (!daemon) return;
        try {
            const st = await daemon.state();
            const t = readTrays(st);
            const sig = traySignature(t);
            trays = t;
            if (sig !== traySig) traySig = sig;
            status = readStatus(st);
            readAt = Date.now();
            stateError = '';
        } catch (e) {
            stateError = e instanceof Error ? e.message : String(e);
        }
    }

    /** Re-read the printer every few seconds while the page is visible (faster during a job). */
    function schedulePoll(): void {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = null;
        if (destroyed || !daemon || document.hidden) return;
        const every = status && isBusy(status.state) ? POLL_JOB_MS : POLL_MS;
        pollTimer = setTimeout(async () => {
            await readPrinter();
            schedulePoll();
        }, every);
    }

    function onVisibility(): void {
        if (!document.hidden && daemon) void readPrinter().then(schedulePoll);
    }

    // ---------------------------------------------------------------- file + preview
    let file = $state<File | null>(null);
    let ftype = $state<FileType>('unknown');
    let model = $state<LoadedModel | null>(null);
    let loadProgress = $state<{ fraction: number; what: string } | null>(null);
    let fileError = $state('');
    let dragging = $state(false);
    let fileSerial = $state(0);
    let meshTray = $state<number | null>(null);
    let fileInput: HTMLInputElement | undefined = $state();

    const isMeshKind = $derived(!!model && (model.kind === 'mesh' || model.kind === 'geometry'));
    const meshSpool = $derived(isMeshKind ? (loaded.find((t) => t.trayId === meshTray) ?? null) : null);
    const palette = $derived(
        model ? (isMeshKind ? [meshSpool?.colour || NEUTRAL] : model.filaments.map((f) => f.colour)) : []
    );

    async function takeFile(f: File | undefined | null): Promise<void> {
        if (!f) return;
        const t = fileType(f.name);
        fileError = '';
        if (t === 'unknown') {
            fileError = `${f.name}: beambam prints .3mf (Bambu Studio / Orca projects or geometry), .gcode.3mf, .stl and .obj files.`;
            return;
        }
        if (f.size > MAX_UPLOAD) {
            fileError = `${f.name} is ${(f.size / 2 ** 20).toFixed(0)} MB; beambam takes files up to 200 MB.`;
            return;
        }
        const serial = ++fileSerial;
        file = f;
        ftype = t;
        model = null;
        resetSlice();
        loadProgress = { fraction: 0, what: 'Reading the file' };
        try {
            const { readModelFile } = await import('$lib/print/worker-client');
            const m = await readModelFile(f, (fraction, what) => {
                if (serial === fileSerial) loadProgress = { fraction, what };
            });
            if (serial !== fileSerial) return;
            model = m;
            if (m.kind === 'mesh' || m.kind === 'geometry') meshTray = meshTray ?? loaded[0]?.trayId ?? null;
        } catch (e) {
            if (serial !== fileSerial) return;
            fileError = `Could not read ${f.name}: ${e instanceof Error ? e.message : String(e)}`;
        } finally {
            if (serial === fileSerial) loadProgress = null;
        }
    }

    function onDrop(e: DragEvent): void {
        e.preventDefault();
        dragging = false;
        void takeFile(e.dataTransfer?.files?.[0]);
    }

    // A mesh file needs a spool once spools are known.
    $effect(() => {
        if (isMeshKind && meshTray === null && loaded.length) meshTray = loaded[0].trayId;
    });

    // ---------------------------------------------------------------- settings
    let settings = $state<PrintSettings>({ ...PROJECT_SETTINGS });
    const settingsKey = $derived(
        JSON.stringify([hasOptions ? settings : null, meshSpool ? [meshSpool.profileId, meshSpool.type] : null])
    );

    /** What "not sent" means for this file: the project's own value, or Bambu's default for a mesh. */
    const defaultLabel = $derived(isMeshKind ? 'Default' : "Project's");

    function setSetting<K extends keyof PrintSettings>(key: K, value: PrintSettings[K]): void {
        settings = { ...settings, [key]: value };
    }

    function numOrNull(v: string): number | null {
        return v === '' ? null : Number(v);
    }

    // ---------------------------------------------------------------- slice / upload
    let slicing = $state(false);
    let sliceTick = $state('');
    let sliceError = $state('');
    let sliceResult = $state<SliceResult | null>(null);
    let slicedKey = $state('');
    let chosenNozzles = $state<number[] | null>(null);
    let printFile = $state<string | null>(null);
    let reslicing = $state(false);
    let resliceNote = $state('');

    const stale = $derived(!!printFile && ftype !== 'gcode.3mf' && slicedKey !== settingsKey);
    const canSlice = $derived(
        !!daemon && !!model && !slicing && ftype !== 'gcode.3mf' && (!isMeshKind || (hasMesh && !!meshSpool))
    );
    const canRenozzle = $derived(hasNozzlePlan && hasOptions && ftype !== 'gcode.3mf');

    function resetSlice(): void {
        sliceResult = null;
        sliceError = '';
        sliceTick = '';
        printFile = null;
        slicedKey = '';
        chosenNozzles = null;
        plan = null;
        startSlots = null;
        resliceNote = '';
        startDone = '';
        startError = '';
        confirmOpen = false;
    }

    /** Slice the file with the current settings (and `nozzles`, when the user moved filaments). */
    async function slice(nozzles: number[] | null = null, what = ''): Promise<void> {
        if (!daemon || !file || !model) return;
        const again = !!what;
        if (again) reslicing = true;
        else slicing = true;
        sliceError = '';
        sliceTick = 'Sending the file to beambam…';
        const key = settingsKey;
        const mesh = isMeshKind && meshSpool ? { profileId: meshSpool.profileId, type: meshSpool.type, colour: meshSpool.colour } : null;
        const opts = hasOptions ? buildSliceOptions(settings, mesh, nozzles) : null;
        try {
            const res = await daemon.slice(file.name, file, opts ? optionsHeader(opts) : null, (sec, step) => {
                sliceTick = `${again ? `Slicing again with ${what}` : 'Slicing'}: ${step || 'working'}… ${Math.round(sec)} s`;
            });
            sliceResult = res;
            printFile = res.file;
            slicedKey = key;
            chosenNozzles = nozzles;
            sliceTick = '';
            resliceNote = again ? `Sliced again with ${what}: the time above is for it.` : '';
        } catch (e) {
            sliceError = e instanceof Error ? e.message : String(e);
            sliceTick = '';
        } finally {
            slicing = false;
            reslicing = false;
        }
    }

    /** An already-sliced file goes straight to the daemon. */
    async function upload(): Promise<void> {
        if (!daemon || !file) return;
        slicing = true;
        sliceError = '';
        sliceTick = 'Sending the print file to beambam…';
        try {
            printFile = await daemon.upload(file.name, file);
            slicedKey = settingsKey;
            sliceTick = '';
        } catch (e) {
            sliceError = e instanceof Error ? e.message : String(e);
            sliceTick = '';
        } finally {
            slicing = false;
        }
    }

    const summary = $derived.by(() => {
        if (sliceResult) return { seconds: sliceResult.seconds, grams: sliceResult.grams, per: sliceResult.gramsPerFilament };
        if (ftype === 'gcode.3mf' && model?.sliced)
            return { seconds: model.sliced.seconds, grams: model.sliced.grams, per: model.filaments.map((f) => f.grams ?? 0) };
        return null;
    });

    // ---------------------------------------------------------------- mapping + start
    let plan = $state<PrintPlan | null>(null);
    let startSlots = $state<number[] | null>(null);
    let confirmOpen = $state(false);
    let plateClean = $state(false);
    let starting = $state(false);
    let startTick = $state('');
    let startError = $state('');
    let startDone = $state('');
    let startedHere = $state(false);

    const printerBusy = $derived(!!status && isBusy(status.state));
    const startBlock = $derived.by(() => {
        if (!daemon) return 'Connect to beambam first.';
        if (!printFile || !plan) return 'Slice the file first.';
        if (stale) return 'The settings changed since this slice: slice again.';
        if (reslicing || slicing) return 'Slicing…';
        if (plan.errors.length || !startSlots) return 'Fix the ✗ rows first.';
        if (printerBusy) return `The printer is busy (${stateText(status!.state).toLowerCase()}): wait for the current job to finish.`;
        return '';
    });

    async function startPrint(): Promise<void> {
        if (!daemon || !printFile || !plan || !startSlots || startBlock) return;
        starting = true;
        startError = '';
        startTick = 'Sending to the printer…';
        try {
            // The warnings on screen when Start was pressed are the user's OK; beambam checks the spools again.
            const said = await daemon.print(printFile, startSlots, plan.warnings.length > 0, (sec, what) => {
                startTick = `${what || 'Sending'}… ${Math.round(sec)} s`;
            });
            startedHere = true;
            startDone = said ? `The printer accepted the print: ${said}` : 'The printer accepted the print.';
            confirmOpen = false;
            plateClean = false;
            await readPrinter();
            schedulePoll();
        } catch (e) {
            startError = e instanceof Error ? e.message : String(e);
        } finally {
            starting = false;
            startTick = '';
        }
    }

    // ---------------------------------------------------------------- lifecycle
    onMount(() => {
        origin = location.origin;
        let auto = false;
        try {
            const raw = localStorage.getItem(STORE_KEY);
            if (raw) {
                const v = JSON.parse(raw) as { url?: string; auto?: boolean };
                if (typeof v.url === 'string' && v.url) urlInput = v.url;
                auto = v.auto === true;
            }
        } catch {
            /* nothing remembered */
        }
        document.addEventListener('visibilitychange', onVisibility);
        // Only reconnect for someone who connected before: probing the device for every visitor
        // would show them the browser's "reach apps on this device" prompt unasked.
        if (auto) void connect();
    });

    onDestroy(() => {
        destroyed = true;
        if (pollTimer) clearTimeout(pollTimer);
        if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
    });

    const pct = (f: number): string => `${Math.round(Math.min(1, Math.max(0, f)) * 100)}%`;
    const units = $derived.by(() => {
        const groups = new Map<string, Tray[]>();
        for (const t of trays) {
            const k = t.unitType === 'External' ? 'External' : t.amsId >= 128 ? `${t.unitType} ${t.amsId - 127}` : `${t.unitType} ${String.fromCharCode(65 + t.amsId)}`;
            groups.set(k, [...(groups.get(k) ?? []), t]);
        }
        return [...groups.entries()];
    });
</script>

<svelte:head>
    <title>Print — beambam</title>
    <meta name="description" content="Print your own 3MF, STL or OBJ on a Bambu Lab printer through the beambam daemon: preview, slice, map spools, start and watch." />
</svelte:head>

<div class="frame py-[var(--space-3xl)] print-page">
    <div class="label mb-[var(--space-md)]">05 · Print</div>
    <h1 class="mb-[var(--space-lg)] leading-[0.95]" style="font-size: clamp(2.25rem, 1.6rem + 3vw, 3.5rem); letter-spacing: -0.03em;">
        Your file.<br /><span style="color: var(--color-accent);">Your printer.</span>
    </h1>
    <p class="max-w-[62ch] leading-[1.6]" style="color: var(--color-mute);">
        Drop a model, check it in 3D, slice it, match each filament to a loaded spool and start it — through
        the beambam daemon on this device or your LAN. The file goes from this browser to your daemon only;
        beambam.boo never sees it.
    </p>

    <!-- 01 CONNECT ------------------------------------------------------------>
    <section class="pp-step" aria-labelledby="s1">
        <div class="pp-step-head"><span class="pp-step-num">01</span><h2 class="pp-step-title" id="s1">Connect</h2></div>

        {#if !daemon}
            <p class="pp-muted mb-[var(--space-md)]">Start beambam where it can reach your printer (<code>pip install beambam</code> first):</p>
            <CodeBlock code={command} ariaLabel="Copy the beambam start command" />
            <form class="connect" onsubmit={(e) => { e.preventDefault(); void connect(); }}>
                <label class="pp-field">
                    <span>Daemon address</span>
                    <input class="pp-input" type="url" inputmode="url" autocomplete="off" spellcheck="false"
                           bind:value={urlInput} placeholder={DEFAULT_URL} />
                </label>
                <button type="submit" class="pp-btn primary" disabled={connecting}>{connecting ? 'Connecting…' : 'Connect'}</button>
            </form>
            <p class="pp-muted pp-small">
                Chrome and Edge may ask to let this site <strong>reach apps on this device</strong> (Local Network Access):
                allow it, or the page cannot talk to beambam. On another machine, run the daemon with
                <code>--http 0.0.0.0:8765</code> and enter its LAN address.
            </p>
            {#if connError}
                <div class="pp-msg err" role="alert">
                    <div>{connError.message}</div>
                </div>
            {/if}
        {:else}
            <div class="pp-card conn">
                <div class="conn-head">
                    <span class="pill"><span class="pill-dot"></span>beambam {health?.version || ''}</span>
                    <span class="pp-mono pp-small pp-muted">{daemon.url}</span>
                    <button type="button" class="pp-btn" onclick={disconnect}>Disconnect</button>
                </div>
                {#if health?.printerSilent}
                    <div class="pp-msg warn">beambam runs but hears nothing from the printer{health.lastMessageAge !== null ? ` (last message ${Math.round(health.lastMessageAge)} s ago)` : ''}. Is the printer on and on this network?</div>
                {/if}
                {#if !hasOptions || !hasMesh}
                    <div class="pp-msg warn">
                        This beambam cannot {!hasOptions ? 'apply print settings' : ''}{!hasOptions && !hasMesh ? ' or ' : ''}{!hasMesh ? 'slice STL / OBJ files' : ''} yet.
                        Update it: <code>pip install -U beambam</code>, then restart the daemon.
                    </div>
                {/if}
                {#if status}
                    <dl class="pp-kv">
                        <dt>Printer</dt>
                        <dd>{stateText(status.state)}{status.job && isBusy(status.state) ? ` · ${status.job} · ${status.percent}%` : ''}</dd>
                    </dl>
                {/if}
                {#if stateError}<div class="pp-msg err">{stateError}</div>{/if}

                {#if trays.length}
                    <div class="spools" aria-label="Loaded spools">
                        {#each units as [unit, list] (unit)}
                            <div class="unit">
                                <div class="pp-legend">{unit}</div>
                                <ul class="unit-trays">
                                    {#each list as t (t.trayId)}
                                        <li class="tray" title={t.empty ? `${t.label}: empty` : `${t.label}: ${t.type} ${t.colour}${t.name ? ` · ${t.name}` : ''}`}>
                                            <Swatch colour={t.empty ? '' : t.colour} size="lg" />
                                            <span class="pp-mono pp-small">{t.label}</span>
                                            <span class="pp-small pp-muted">{t.empty ? 'empty' : t.type}</span>
                                        </li>
                                    {/each}
                                </ul>
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>
        {/if}
    </section>

    <!-- 02 FILE ---------------------------------------------------------------->
    <section class="pp-step" aria-labelledby="s2">
        <div class="pp-step-head"><span class="pp-step-num">02</span><h2 class="pp-step-title" id="s2">Your file</h2></div>
        <label
            class="drop"
            class:dragging
            ondragover={(e) => { e.preventDefault(); dragging = true; }}
            ondragleave={() => (dragging = false)}
            ondrop={onDrop}
        >
            <input bind:this={fileInput} type="file" class="sr-only"
                   onchange={(e) => takeFile((e.currentTarget as HTMLInputElement).files?.[0])} />
            <span class="drop-title">{file ? file.name : 'Drop a file, or tap to choose'}</span>
            <span class="pp-muted pp-small">
                {#if file}
                    {(file.size / 2 ** 20).toFixed(1)} MB · {ftype === 'gcode.3mf' ? 'already sliced' : ftype.toUpperCase()}{model ? ` · ${model.triangles.toLocaleString()} triangles` : ''} · tap to choose another
                {:else}
                    .3mf (Bambu Studio / Orca project or geometry), .stl, .obj, or a sliced .gcode.3mf
                {/if}
            </span>
        </label>
        {#if loadProgress}
            <div class="load" aria-live="polite">
                <div class="pp-small pp-muted">{loadProgress.what}… {pct(loadProgress.fraction)}</div>
                <div class="pp-bar"><div style:width={pct(loadProgress.fraction)}></div></div>
            </div>
        {/if}
        {#if fileError}<div class="pp-msg err" role="alert">{fileError}</div>{/if}
    </section>

    <!-- 03 PREVIEW + FILAMENTS ------------------------------------------------->
    <section class="pp-step" class:locked={!file} aria-labelledby="s3">
        <div class="pp-step-head"><span class="pp-step-num">03</span><h2 class="pp-step-title" id="s3">Preview</h2></div>
        <Preview {model} {palette} />
        {#if model}
            <div class="fils">
                <div class="pp-legend">{isMeshKind ? 'Filament' : `Filaments (${model.filaments.length})`}</div>
                {#if isMeshKind}
                    <p class="pp-muted pp-small">One filament: pick the loaded spool it prints from (that also sets its material).</p>
                    {#if loaded.length}
                        <SpoolPicker
                            choices={spoolChoices(trays, '', meshTray)}
                            value={meshTray}
                            label="The model's filament"
                            showMatch={false}
                            disabled={slicing || reslicing}
                            onpick={(t) => (meshTray = t)}
                        />
                    {:else}
                        <div class="pp-msg info">Connect to beambam to see the loaded spools.</div>
                    {/if}
                {:else}
                    <ul class="fil-list">
                        {#each model.filaments as f, i (i)}
                            <li>
                                <Swatch colour={f.colour} text={String(i + 1)} />
                                <span class="pp-mono pp-small">{f.type || '—'}</span>
                                <span class="pp-muted pp-small">{f.colour}</span>
                                {#if summary?.per?.[i]}<span class="pp-small fil-g">{gramsText(summary.per[i])}</span>{/if}
                            </li>
                        {/each}
                    </ul>
                {/if}
            </div>
        {/if}
    </section>

    <!-- 04 SETTINGS ------------------------------------------------------------>
    <section class="pp-step" class:locked={!model || ftype === 'gcode.3mf'} aria-labelledby="s4">
        <div class="pp-step-head"><span class="pp-step-num">04</span><h2 class="pp-step-title" id="s4">Settings</h2></div>
        {#if ftype === 'gcode.3mf'}
            <p class="pp-muted">This file is already sliced: its settings are fixed.</p>
        {:else}
            {#if daemon && !hasOptions}
                <div class="pp-msg warn">This beambam slices with the {isMeshKind ? 'default' : "project's"} settings only. Update it to change them here.</div>
            {/if}
            <fieldset class="settings" disabled={!!daemon && !hasOptions}>
                <legend class="sr-only">Print settings</legend>
                <div class="pp-field">
                    <span class="pp-legend" id="lbl-sup">Supports</span>
                    <div class="pp-seg" style="--cols: 2" role="radiogroup" aria-labelledby="lbl-sup">
                        {#each SUPPORT_MODES_UI as o (o.label)}
                            <button type="button" role="radio" aria-checked={settings.supports === o.value}
                                    onclick={() => setSetting('supports', o.value)}>{o.value === null ? defaultLabel : o.label}</button>
                        {/each}
                    </div>
                </div>
                <div class="pp-field">
                    <span class="pp-legend" id="lbl-brim">Brim</span>
                    <div class="pp-seg" style="--cols: 2" role="radiogroup" aria-labelledby="lbl-brim">
                        {#each BRIM_MODES_UI as o (o.label)}
                            <button type="button" role="radio" aria-checked={settings.brim === o.value}
                                    onclick={() => setSetting('brim', o.value)}>{o.value === null ? defaultLabel : o.label}</button>
                        {/each}
                    </div>
                </div>
                <div class="settings-nums">
                    <label class="pp-field">
                        <span>Layer height</span>
                        <select class="pp-select" value={settings.layerHeight ?? ''}
                                onchange={(e) => setSetting('layerHeight', numOrNull(e.currentTarget.value))}>
                            <option value="">{isMeshKind ? 'Default (0.20)' : "Project's"}</option>
                            {#each LAYER_HEIGHTS as h (h)}<option value={h}>{h.toFixed(2)} mm</option>{/each}
                        </select>
                    </label>
                    <label class="pp-field">
                        <span>Walls</span>
                        <select class="pp-select" value={settings.walls ?? ''}
                                onchange={(e) => setSetting('walls', numOrNull(e.currentTarget.value))}>
                            <option value="">{isMeshKind ? 'Default' : "Project's"}</option>
                            {#each WALL_COUNTS as w (w)}<option value={w}>{w}</option>{/each}
                        </select>
                    </label>
                    <label class="pp-field">
                        <span>Infill</span>
                        <select class="pp-select" value={settings.infill ?? ''}
                                onchange={(e) => setSetting('infill', numOrNull(e.currentTarget.value))}>
                            <option value="">{isMeshKind ? 'Default' : "Project's"}</option>
                            {#each INFILL_STEPS as v (v)}<option value={v}>{v} %</option>{/each}
                        </select>
                    </label>
                </div>
                {#if hasSettings(settings)}
                    <button type="button" class="pp-btn" onclick={() => (settings = { ...PROJECT_SETTINGS })}>
                        Back to the {isMeshKind ? 'defaults' : "project's settings"}
                    </button>
                {/if}
            </fieldset>
        {/if}
    </section>

    <!-- 05 SLICE --------------------------------------------------------------->
    <section class="pp-step" class:locked={!model} aria-labelledby="s5">
        <div class="pp-step-head"><span class="pp-step-num">05</span><h2 class="pp-step-title" id="s5">{ftype === 'gcode.3mf' ? 'Send' : 'Slice'}</h2></div>
        {#if ftype === 'gcode.3mf'}
            <button type="button" class="pp-btn primary wide" disabled={!daemon || !model || slicing || !!printFile} onclick={upload}>
                {printFile ? 'Sent to beambam' : slicing ? 'Sending…' : 'Send to beambam'}
            </button>
        {:else}
            {#if isMeshKind && daemon && !hasMesh}
                <div class="pp-msg warn">This beambam slices Bambu Studio projects only. Update it (<code>pip install -U beambam</code>) to slice STL / OBJ files.</div>
            {/if}
            <button type="button" class="pp-btn primary wide" disabled={!canSlice || reslicing} onclick={() => slice(null)}>
                {slicing ? 'Slicing…' : printFile && !stale ? 'Slice again' : 'Slice'}
            </button>
            {#if !daemon && model}<p class="pp-muted pp-small">Connect to beambam (step 01) to slice.</p>{/if}
        {/if}
        {#if sliceTick}<div class="pp-msg info" aria-live="polite">{sliceTick}</div>{/if}
        {#if sliceError}<div class="pp-msg err" role="alert">{sliceError}</div>{/if}
        {#if stale}<div class="pp-msg warn">The settings or spool changed since this slice: slice again before printing.</div>{/if}
        {#if summary && printFile}
            <dl class="pp-kv slice-sum">
                <dt>Time</dt><dd>{secondsText(summary.seconds)}</dd>
                <dt>Filament</dt><dd>{gramsText(summary.grams)}</dd>
                {#if chosenNozzles}<dt>Nozzles asked</dt><dd>{chosenNozzles.map((n, i) => `${i + 1}:${n === 1 ? 'L' : 'R'}`).join(' ')}</dd>{/if}
            </dl>
            {#if summary.grams <= 0}
                <div class="pp-msg warn">beambam reports no filament used: the model may be too small or too thin to print. Check its size and the layer height.</div>
            {/if}
            {#if resliceNote}<div class="pp-msg ok">{resliceNote}</div>{/if}
            {#each sliceResult?.warnings ?? [] as w}<div class="pp-msg warn">⚠ {w}</div>{/each}
        {/if}
    </section>

    <!-- 06 SPOOLS -------------------------------------------------------------->
    <section class="pp-step" class:locked={!printFile} aria-labelledby="s6">
        <div class="pp-step-head"><span class="pp-step-num">06</span><h2 class="pp-step-title" id="s6">Spools</h2></div>
        {#if daemon && printFile}
            {#key file}
                <MappingPanel
                    {daemon}
                    file={printFile}
                    {trays}
                    {traySig}
                    canRenozzle={canRenozzle}
                    nozzles={chosenNozzles}
                    {reslicing}
                    grams={summary?.per ?? []}
                    initialSlots={isMeshKind && meshTray !== null ? [meshTray] : null}
                    locked={starting}
                    onrenozzle={(n, what) => slice(n, what)}
                    onchange={(p, s) => { plan = p; startSlots = s; }}
                />
            {/key}
        {:else}
            <p class="pp-muted">After slicing, each filament is matched to a loaded spool here.</p>
        {/if}
    </section>

    <!-- 07 PRINT --------------------------------------------------------------->
    <section class="pp-step" class:locked={!plan && !printerBusy} aria-labelledby="s7">
        <div class="pp-step-head"><span class="pp-step-num">07</span><h2 class="pp-step-title" id="s7">Print</h2></div>
        {#if !confirmOpen}
            <button type="button" class="pp-btn primary wide" disabled={!!startBlock || starting} onclick={() => { confirmOpen = true; plateClean = false; }}>
                Start print…
            </button>
            {#if startBlock && plan}<p class="pp-muted pp-small">{startBlock}</p>{/if}
        {:else}
            <div class="pp-card confirm" role="dialog" aria-labelledby="confirm-title">
                <div class="pp-step-title" id="confirm-title" style="font-size: var(--text-lg);">Before it starts</div>
                <ul class="confirm-list pp-small">
                    <li>File: <span class="pp-mono">{file?.name}</span>{summary ? ` · ${secondsText(summary.seconds)} · ${gramsText(summary.grams)}` : ''}</li>
                    <li>Spools: {plan?.filaments.map((f) => `${f.filament}→${f.tray ? f.tray.label.replace(/\s*\(slot \d+\)$/, '') : '?'}`).join(', ')}</li>
                    {#if plan?.warnings.length}<li class="pp-warn-text">Starting accepts {plan.warnings.length} ⚠ warning{plan.warnings.length > 1 ? 's' : ''}.</li>{/if}
                </ul>
                <label class="check">
                    <input type="checkbox" bind:checked={plateClean} />
                    <span>The build plate is clean and empty.</span>
                </label>
                <div class="pp-row">
                    <button type="button" class="pp-btn primary" disabled={!plateClean || starting || !!startBlock} onclick={startPrint}>
                        {starting ? 'Starting…' : 'Start print'}
                    </button>
                    <button type="button" class="pp-btn" disabled={starting} onclick={() => (confirmOpen = false)}>Cancel</button>
                </div>
                {#if startBlock}<p class="pp-muted pp-small">{startBlock}</p>{/if}
            </div>
        {/if}
        {#if startTick}<div class="pp-msg info" aria-live="polite">{startTick}</div>{/if}
        {#if startError}<div class="pp-msg err" role="alert">{startError}</div>{/if}
        {#if startDone}<div class="pp-msg ok">{startDone}</div>{/if}

        {#if daemon && status && (isBusy(status.state) || startedHere || status.state === 'FINISH' || status.state === 'FAILED')}
            <div class="job-wrap">
                <div class="pp-legend">On the printer now</div>
                <JobView {status} {daemon} {startedHere} {readAt} />
            </div>
        {/if}
    </section>
</div>

<style>
    .print-page { max-width: 46rem; }
    .connect { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: var(--space-sm); align-items: end; margin-block: var(--space-lg) var(--space-md); }
    .conn { display: grid; gap: var(--space-md); }
    .conn-head { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-sm) var(--space-md); }
    .conn-head .pp-btn { margin-left: auto; }
    .spools { display: grid; grid-template-columns: repeat(auto-fill, minmax(9.5rem, 1fr)); gap: var(--space-md) var(--space-lg); }
    .unit-trays { list-style: none; margin: var(--space-xs) 0 0; padding: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--space-sm); }
    .tray { display: grid; justify-items: center; gap: 2px; text-align: center; min-width: 0; }
    .drop {
        display: grid;
        gap: var(--space-xs);
        place-items: center;
        text-align: center;
        min-height: 128px;
        padding: var(--space-xl) var(--space-lg);
        border: 1px dashed var(--color-mute-2);
        background: var(--color-surface-3);
        cursor: pointer;
        transition: border-color 0.15s ease, background 0.15s ease;
    }
    .drop:hover, .drop.dragging, .drop:focus-within { border-color: var(--color-accent); background: color-mix(in oklab, var(--color-accent) 6%, var(--color-surface-3)); }
    .drop:focus-within { outline: 2px solid var(--color-accent); outline-offset: 2px; }
    .drop-title { font-family: var(--font-mono); font-size: var(--text-sm); overflow-wrap: anywhere; }
    .load { display: grid; gap: var(--space-xs); margin-top: var(--space-md); }
    .fils { margin-top: var(--space-lg); display: grid; gap: var(--space-sm); }
    .fil-list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--space-sm); }
    .fil-list li { display: grid; grid-template-columns: auto auto minmax(0, 1fr) auto; align-items: center; gap: var(--space-md); }
    .fil-g { font-family: var(--font-mono); }
    .settings { border: 0; padding: 0; margin: 0; display: grid; gap: var(--space-lg); min-width: 0; }
    .settings:disabled { opacity: 0.5; }
    .settings-nums { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--space-sm); }
    .slice-sum { margin-top: var(--space-md); }
    .confirm { display: grid; gap: var(--space-md); border-color: var(--color-accent); }
    .confirm-list { margin: 0; padding-left: 1.1rem; display: grid; gap: var(--space-xs); overflow-wrap: anywhere; }
    .check { display: flex; align-items: center; gap: var(--space-md); min-height: 44px; cursor: pointer; font-size: var(--text-sm); }
    .check input { width: 24px; height: 24px; accent-color: var(--color-accent); flex-shrink: 0; }
    .job-wrap { margin-top: var(--space-xl); display: grid; gap: var(--space-sm); }
    .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
    @media (max-width: 400px) {
        .settings-nums { grid-template-columns: 1fr 1fr; }
        .settings-nums label:first-child { grid-column: 1 / -1; }
    }
</style>
