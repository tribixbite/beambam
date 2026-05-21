<script lang="ts">
    // Numbers carry the brand. These are real.
    const stats = [
        { n: '28', label: 'CLI subcommands' },
        { n: '12', label: 'printer models supported' },
        { n: '300', label: 'tests passing offline' },
        { n: '~69', label: 'Home Assistant entities' },
        { n: '25', label: 'MCP tools for Claude / Cursor' },
        { n: 'MIT', label: 'license for the package' }
    ];

    // A real install transcript, terminal-style.
    const install = `# 1.  Install (any Linux / macOS / WSL / Termux)
pip install beambam

# 2.  Interactive setup wizard: discover printer + write creds.
beambam init

# 3.  Verify everything's healthy.
beambam doctor

# 4.  Preview a print plan before sending it hot.
beambam analyze model.gcode.3mf

# 5.  Upload + start print, AMS slot 5.
beambam print model.gcode.3mf --slot 5`;

    // Realistic doctor output — the same one that surfaced during the
    // eevee print. This is the marketing.
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
            body: 'beambam analyze model.3mf dissects filament/nozzle assignment, walks the layer phases, counts real flushes (not cosmetic tool-switches), reports total purge volume in mm and grams, and surfaces hints when a tri-color middle is about to burn 24g of filament on a wipe tower.'
        },
        {
            tag: 'PORTABLE',
            head: 'Pure Python wheel. Termux to homelab.',
            body: 'Tested in CI on Python 3.10\u20133.13 on Ubuntu + macOS. The bridge runs anywhere paho-mqtt + cryptography do \u2014 including aarch64 Android phones via Termux, where the official Bambu Studio has never shipped a working Network Plugin.'
        }
    ];
</script>

<!-- ====================================================================
     HERO — the wordmark IS the page. Blueprint-aligned.
     ==================================================================== -->
<section class="border-b border-[var(--color-hair)]">
    <div class="frame py-[var(--space-5xl)] grid gap-[var(--space-2xl)] md:grid-cols-12">
        <div class="md:col-span-12">
            <div class="label">Section 01 &middot; LAN-first stack &middot; Bambu Lab printers</div>
        </div>

        <h1 class="md:col-span-12 mb-0">
            <span style="color: var(--color-accent);">beam</span><span style="color: var(--color-ink);">bam</span>
        </h1>

        <div class="md:col-span-7">
            <p class="text-[var(--text-lg)] leading-[1.45] max-w-[55ch]" style="color: var(--color-ink);">
                Signed-MQTT bridge and daemon stack for every Bambu Lab printer.
                Pure Python. No cloud account. Runs anywhere paho-mqtt does \u2014
                Linux, macOS, WSL, Android via Termux.
            </p>
            <div class="mt-[var(--space-xl)] flex flex-wrap items-center gap-[var(--space-md)]">
                <a href="/cli"
                   class="inline-block px-[var(--space-xl)] py-[var(--space-md)] no-underline font-[var(--font-mono)] uppercase tracking-widest text-[var(--text-xs)]"
                   style="text-decoration: none; background: var(--color-accent); color: var(--color-surface);">
                    Command index &nbsp;\u2192
                </a>
                <a href="https://github.com/tribixbite/beambam"
                   class="inline-block px-[var(--space-xl)] py-[var(--space-md)] no-underline font-[var(--font-mono)] uppercase tracking-widest text-[var(--text-xs)]"
                   target="_blank" rel="noopener"
                   style="text-decoration: none; border: 1px solid var(--color-hair); color: var(--color-ink);">
                    View source &nbsp;\u2197
                </a>
            </div>
        </div>

        <!-- Right column: install transcript -->
        <div class="md:col-span-5 md:col-start-8">
            <div class="label mb-[var(--space-sm)]">Install &amp; first print &middot; total &lt; 90s</div>
            <pre><code>{install}</code></pre>
        </div>
    </div>
</section>

<!-- ====================================================================
     STATS — numbers do the work that adjectives would in a worse design.
     ==================================================================== -->
<section class="border-b border-[var(--color-hair)]">
    <div class="frame py-[var(--space-3xl)]">
        <div class="label mb-[var(--space-xl)]">Section 02 &middot; By the numbers</div>
        <dl class="grid grid-cols-2 md:grid-cols-6 gap-y-[var(--space-2xl)] gap-x-[var(--space-xl)]">
            {#each stats as { n, label }, i}
                <div class="pl-[var(--space-lg)]" style="border-left: 1px solid var(--color-hair);">
                    <dt class="font-[var(--font-display)] text-[var(--text-2xl)] leading-none tabular-nums" style="color: var(--color-ink);">{n}</dt>
                    <dd class="mt-[var(--space-sm)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest" style="color: var(--color-mute);">{label}</dd>
                </div>
            {/each}
        </dl>
    </div>
</section>

<!-- ====================================================================
     CLAIMS — four real things, not six rounded cards.
     ==================================================================== -->
<section class="border-b border-[var(--color-hair)]">
    <div class="frame py-[var(--space-5xl)]">
        <div class="label mb-[var(--space-2xl)]">Section 03 &middot; What it does</div>

        <div class="grid md:grid-cols-12 gap-y-[var(--space-3xl)] gap-x-[var(--space-2xl)]">
            {#each claims as { tag, head, body }, i}
                <div class="md:col-span-12 md:grid md:grid-cols-12 md:gap-x-[var(--space-2xl)]">
                    <div class="md:col-span-2 mb-[var(--space-sm)] md:mb-0">
                        <div class="badge inline-block">{String(i + 1).padStart(2, '0')} \xb7 {tag}</div>
                    </div>
                    <h3 class="md:col-span-5 mb-[var(--space-sm)] md:mb-0" style="color: var(--color-ink);">{head}</h3>
                    <p class="md:col-span-5 leading-[1.65] max-w-[60ch]" style="color: var(--color-mute);">{body}</p>
                </div>
                {#if i < claims.length - 1}
                    <hr class="hr-dashed md:col-span-12" style="margin: 0;" />
                {/if}
            {/each}
        </div>
    </div>
</section>

<!-- ====================================================================
     DOCTOR OUTPUT — don't tell, show. Real terminal output.
     ==================================================================== -->
<section class="border-b border-[var(--color-hair)]">
    <div class="frame py-[var(--space-5xl)] grid md:grid-cols-12 gap-[var(--space-2xl)]">
        <div class="md:col-span-4">
            <div class="label mb-[var(--space-md)]">Section 04 &middot; Sample output</div>
            <h2 class="mb-[var(--space-lg)]" style="color: var(--color-ink);">Doctor.</h2>
            <p class="max-w-[40ch] leading-[1.6]" style="color: var(--color-mute);">
                One command, six categories, semantic exit code. Returns
                <code>0</code> when everything's green, <code>1</code> on warnings,
                <code>2</code> on hard failures. Wire it into your night-print
                pre-flight script.
            </p>
            <p class="mt-[var(--space-md)] max-w-[40ch] leading-[1.6]" style="color: var(--color-mute);">
                Below: a real run captured mid-print on an X2D with two AMS units
                showing damp filament.
            </p>
        </div>
        <div class="md:col-span-8">
            <pre><code>{doctorSample}</code></pre>
        </div>
    </div>
</section>

<!-- ====================================================================
     CLOSING — single emphasis on the install line.
     ==================================================================== -->
<section>
    <div class="frame py-[var(--space-5xl)] text-center">
        <div class="label mb-[var(--space-md)]">Section 05 &middot; Try it</div>
        <h2 class="mb-[var(--space-xl)]" style="color: var(--color-ink);">
            Three lines from <span style="color: var(--color-accent);">zero</span> to first print.
        </h2>
        <div class="inline-block text-left">
            <pre style="font-size: var(--text-base);"><code>pip install beambam
beambam init
beambam print model.3mf --slot 1</code></pre>
        </div>
        <div class="mt-[var(--space-xl)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest" style="color: var(--color-mute);">
            Full reference: <a href="/cli" style="color: var(--color-ink);">/cli</a>
            &nbsp;&middot;&nbsp;
            Library API: <a href="/docs" style="color: var(--color-ink);">/docs</a>
        </div>
    </div>
</section>
