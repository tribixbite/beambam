<script lang="ts">
    const stats = [
        { n: '28', label: 'CLI subcommands' },
        { n: '12', label: 'printer models' },
        { n: '300', label: 'tests passing' },
        { n: '~69', label: 'HA entities' },
        { n: '25', label: 'MCP tools' },
        { n: 'MIT', label: 'license' }
    ];

    const install = `# 1.  Install (Linux / macOS / WSL / Termux)
pip install beambam

# 2.  Interactive wizard: discover printer + write creds.
beambam init

# 3.  Verify everything's healthy.
beambam doctor

# 4.  Preview a print plan before sending it hot.
beambam analyze model.gcode.3mf

# 5.  Upload + start print, AMS slot 5.
beambam print model.gcode.3mf --slot 5`;

    // Doctor output uses unicode literals — inside a JS string, \u is processed.
    const doctorSample = `$ beambam doctor

AMS
  \u2713 unit 0 humidity              level 2/4
  \u26A0 unit 2 humidity              level 3/4 \u2014 getting damp; consider drying
  \u26A0 unit 3 humidity              level 3/4 \u2014 getting damp; consider drying

Camera
  \u2713 ipcam                        1080p record=disable

Errors
  \u2713 active HMS codes             no active errors

Job
  \xb7 print state                  RUNNING 67% layer 165/248 ETA 248min

Network
  \u2713 wifi signal                  \u221252dBm

Sensors
  \u2713 bed thermistor               45.0\xB0C
  \u2713 nozzle thermistor            220.0\xB0C

Summary: 6 pass, 2 warn, 0 fail`;

    const claims = [
        {
            tag: 'BRIDGE',
            head: 'Signed MQTT over the LAN. No cloud account.',
            body: 'Bambu firmware since Jan-2025 requires RSA-SHA256 signed MQTT for every command. beambam signs transparently using the publicly-leaked Bambu Connect cert. Your printer never has to phone home, your access code never leaves your network, and the bridge speaks the same wire format the official Studio + Network Plugin do.'
        },
        {
            tag: 'INTEGRATIONS',
            head: 'Home Assistant, MCP, Web UI, Prometheus, WebRTC.',
            body: 'One daemon publishes everything. ~69 HA entities via MQTT discovery (per-tray AMS color + humidity warnings). 25 MCP tools for Claude / Cursor / Continue. Mobile-friendly web UI with multi-printer queue. Prometheus /metrics for SREs. WebRTC chamber camera under 100ms latency.'
        },
        {
            tag: 'ANALYZER',
            head: 'Read the print plan before the printer does.',
            body: 'beambam analyze model.3mf dissects filament and nozzle assignment, walks the layer phases, counts real flushes (not cosmetic tool-switches), reports total purge volume in mm and grams, and warns when a tri-color middle is about to burn 24 grams of filament on a wipe tower.'
        },
        {
            tag: 'PORTABLE',
            head: 'Pure-Python wheel. Termux to homelab.',
            body: 'Tested in CI on Python 3.10–3.13 on Ubuntu + macOS. The bridge runs anywhere paho-mqtt + cryptography do — including aarch64 Android phones via Termux, where the official Bambu Studio has never shipped a working Network Plugin.'
        }
    ];
</script>

<!-- ====================================================================
     HERO — wordmark IS the page. Mobile-first; hero stacks at all sizes
     below md; install transcript flows below on mobile (full width).
     ==================================================================== -->
<section style="border-bottom: 1px solid var(--color-hair);">
    <div class="frame py-[var(--space-3xl)] sm:py-[var(--space-5xl)]">

        <!-- Status pill — "we shipped a thing" -->
        <div class="pill mb-[var(--space-2xl)]">
            <span class="pill-dot"></span>
            <span>v1.2.0 shipped · 12 new commands</span>
        </div>

        <!-- Wordmark — its own row so it can breathe. Hero size capped at
             3.5rem mobile, scales up to ~9rem on huge screens. -->
        <h1 class="mb-[var(--space-xl)]">
            <span style="color: var(--color-accent);">beam</span><span style="color: var(--color-ink);">bam</span>
        </h1>

        <!-- 2-column at md+, stacked below. Keeps install pre below hero copy on mobile. -->
        <div class="grid gap-[var(--space-2xl)] md:grid-cols-12 md:gap-x-[var(--space-3xl)] md:gap-y-[var(--space-3xl)]">
            <div class="md:col-span-7">
                <p class="leading-[1.5] max-w-[55ch]" style="color: var(--color-ink); font-size: var(--text-lg);">
                    Signed-MQTT bridge and daemon stack for every Bambu Lab printer.
                    Pure Python. No cloud account. Runs anywhere paho-mqtt does —
                    Linux, macOS, WSL, Android via Termux.
                </p>
                <div class="mt-[var(--space-xl)] flex flex-wrap gap-[var(--space-md)]">
                    <a href="/cli" class="btn-primary">Command index →</a>
                    <a href="https://github.com/tribixbite/beambam" target="_blank" rel="noopener" class="btn-secondary">View source ↗</a>
                </div>
            </div>

            <div class="md:col-span-5 min-w-0">
                <div class="label mb-[var(--space-sm)]">Install &amp; first print · &lt; 90s</div>
                <pre><code>{install}<span class="cursor"></span></code></pre>
            </div>
        </div>
    </div>
</section>

<!-- ====================================================================
     STATS RIBBON — 3 cols mobile, 6 cols desktop. Numbers carry brand.
     ==================================================================== -->
<section style="border-bottom: 1px solid var(--color-hair);">
    <div class="frame py-[var(--space-2xl)]">
        <div class="label mb-[var(--space-xl)]">02 · By the numbers</div>
        <dl class="grid grid-cols-3 sm:grid-cols-3 md:grid-cols-6 gap-y-[var(--space-xl)] gap-x-[var(--space-md)] sm:gap-x-[var(--space-xl)]">
            {#each stats as { n, label }}
                <div class="pl-[var(--space-md)]" style="border-left: 1px solid var(--color-hair);">
                    <dt class="font-[var(--font-display)] leading-none tabular-nums"
                        style="color: var(--color-ink); font-size: clamp(1.75rem, 1.2rem + 2vw, 2.75rem);">{n}</dt>
                    <dd class="mt-[var(--space-sm)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest leading-tight"
                        style="color: var(--color-mute);">{label}</dd>
                </div>
            {/each}
        </dl>
    </div>
</section>

<!-- ====================================================================
     CLAIMS — 4 real things in row layout (badge, head, body).
     Mobile: each claim stacks vertically.
     ==================================================================== -->
<section style="border-bottom: 1px solid var(--color-hair);">
    <div class="frame py-[var(--space-4xl)]">
        <div class="label mb-[var(--space-2xl)]">03 · What it does</div>

        <div class="grid gap-y-[var(--space-3xl)]">
            {#each claims as { tag, head, body }, i}
                <div class="grid md:grid-cols-12 gap-[var(--space-md)] md:gap-x-[var(--space-2xl)]">
                    <div class="md:col-span-2">
                        <div class="badge">{String(i + 1).padStart(2, '0')} · {tag}</div>
                    </div>
                    <h3 class="md:col-span-5 mt-[var(--space-xs)] md:mt-0" style="color: var(--color-ink);">{head}</h3>
                    <p class="md:col-span-5 leading-[1.65] max-w-[60ch]" style="color: var(--color-mute);">{body}</p>
                </div>
                {#if i < claims.length - 1}
                    <hr class="hr-dashed" style="margin: 0;" />
                {/if}
            {/each}
        </div>
    </div>
</section>

<!-- ====================================================================
     DOCTOR OUTPUT — show, don't tell. Real terminal output as the
     marketing. Mobile: copy first, pre below.
     ==================================================================== -->
<section style="border-bottom: 1px solid var(--color-hair);">
    <div class="frame py-[var(--space-4xl)] grid gap-[var(--space-2xl)] md:grid-cols-12">
        <div class="md:col-span-4">
            <div class="label mb-[var(--space-md)]">04 · Sample output</div>
            <h2 class="mb-[var(--space-lg)]" style="color: var(--color-ink);">Doctor.</h2>
            <p class="max-w-[40ch] leading-[1.6]" style="color: var(--color-mute);">
                One command, six categories, semantic exit code. Returns
                <code>0</code> when everything's green, <code>1</code> on warnings,
                <code>2</code> on hard failures. Wire it into your night-print
                pre-flight script.
            </p>
            <p class="mt-[var(--space-md)] max-w-[40ch] leading-[1.6]" style="color: var(--color-mute);">
                Below: a real run captured mid-print on an X2D with two AMS
                units showing damp filament.
            </p>
        </div>
        <div class="md:col-span-8 min-w-0">
            <pre><code>{doctorSample}</code></pre>
        </div>
    </div>
</section>

<!-- ====================================================================
     CLOSING — one strong CTA repeating the install line.
     ==================================================================== -->
<section>
    <div class="frame py-[var(--space-4xl)] text-center">
        <div class="label mb-[var(--space-md)]">05 · Try it</div>
        <h2 class="mb-[var(--space-xl)]" style="color: var(--color-ink);">
            Three lines from <span style="color: var(--color-accent);">zero</span> to first print.
        </h2>
        <div class="inline-block text-left max-w-full">
            <pre style="font-size: clamp(0.875rem, 0.8rem + 0.3vw, 1rem);"><code>pip install beambam
beambam init
beambam print model.3mf --slot 1</code></pre>
        </div>
        <div class="mt-[var(--space-xl)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest flex flex-wrap justify-center gap-x-[var(--space-md)] gap-y-[var(--space-sm)]" style="color: var(--color-mute);">
            <span>Full reference: <a href="/cli" style="color: var(--color-ink);">/cli</a></span>
            <span>·</span>
            <span>Library API: <a href="/docs" style="color: var(--color-ink);">/docs</a></span>
        </div>
    </div>
</section>
