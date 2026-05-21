<script lang="ts">
    const commands = [
        { cmd: 'beambam status', desc: 'Pull live printer state' },
        { cmd: 'beambam analyze model.3mf', desc: 'Dissect a print: filaments, flush volume, hints' },
        { cmd: 'beambam ams status', desc: 'Pretty AMS state — colors, humidity, slots' },
        { cmd: 'beambam doctor', desc: 'Full health diagnostic — HMS, AMS, signal, sensors' },
        { cmd: 'beambam cam watch', desc: 'Terminal camera viewer (kitty / iTerm2 / blocks)' },
        { cmd: 'beambam find', desc: 'SSDP LAN discovery' },
        { cmd: 'beambam init', desc: 'Interactive first-run wizard' }
    ];

    const features = [
        {
            title: 'Every Bambu Lab printer',
            desc: 'X1 / X1C / X1E, P1P / P1S / P2S, A1 / A1 mini, H2D / H2S / H2C, X2D. Signed MQTT for Jan-2025+ firmware works transparently — no cloud account, no Bambu Connect plugin.'
        },
        {
            title: 'Cross-platform',
            desc: 'Pure-Python wheel: Linux, macOS, Windows (WSL), Android (Termux). Tested on Python 3.10–3.13 in CI.'
        },
        {
            title: 'No cloud lock-in',
            desc: 'LAN-only by default. Bambu Cloud is optional — log in for MakerWorld design metadata + cloud-print history, never required for LAN control.'
        },
        {
            title: 'Stack-ready',
            desc: 'Home Assistant publisher (~69 entities). MCP server for Claude/Cursor/Continue. Daemon with REST/SSE/Prometheus. Multi-printer queue. Timelapse recorder. AMS color-sync. WebRTC chamber camera.'
        },
        {
            title: 'Print-plan analyzer',
            desc: '<code>beambam analyze file.3mf</code> dissects filament/nozzle assignment, per-phase toolchanges, real flush volume in mm + grams, AMS-tray requirements, and warns about flush-heavy tri-color middles.'
        },
        {
            title: 'Library + CLI',
            desc: '<code>import beambam</code> for typed library API (Printer, Creds, sign_payload, schemas). 28 CLI subcommands across pull/push/inspect/health.'
        }
    ];
</script>

<!-- Hero -->
<section class="border-b border-steel-700 py-20 px-6">
    <div class="max-w-6xl mx-auto">
        <h1 class="text-5xl sm:text-7xl font-bold tracking-tight">
            <span class="text-flame-400">beam</span>bam
        </h1>
        <p class="mt-4 text-xl sm:text-2xl text-stone-400 max-w-2xl">
            LAN-first stack for Bambu Lab 3D printers. <em>Beam</em> (send) + <em>bam</em> (fast). Pure Python, no cloud account, no Bambu Connect plugin.
        </p>

        <div class="mt-8 flex flex-wrap gap-3">
            <a href="/docs" class="bg-flame-500 hover:bg-flame-400 text-stone-950 px-5 py-2.5 rounded font-medium no-underline">
                Get started →
            </a>
            <a href="https://github.com/tribixbite/beambam" target="_blank" rel="noopener" class="border border-steel-600 hover:border-stone-400 text-stone-100 hover:text-flame-300 px-5 py-2.5 rounded font-medium no-underline">
                GitHub
            </a>
        </div>

        <div class="mt-10 max-w-2xl">
            <p class="text-sm text-stone-500 mb-2">Install on any Linux / macOS / WSL:</p>
            <pre class="text-flame-300"><code>pip install beambam
beambam init                # interactive setup wizard</code></pre>
        </div>
    </div>
</section>

<!-- Featured commands -->
<section class="py-16 px-6 border-b border-steel-700">
    <div class="max-w-6xl mx-auto">
        <h2 class="text-3xl font-bold mb-2">28 subcommands. One CLI.</h2>
        <p class="text-stone-400 mb-8">Each verb is also importable as <code>beambam.X</code>. Highlights:</p>
        <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {#each commands as { cmd, desc }}
                <div class="border border-steel-700 rounded-lg p-4 bg-steel-800/40 hover:border-flame-500 transition">
                    <code class="text-flame-300 text-sm block mb-2">{cmd}</code>
                    <p class="text-stone-400 text-sm">{desc}</p>
                </div>
            {/each}
        </div>
    </div>
</section>

<!-- Why beambam -->
<section class="py-16 px-6">
    <div class="max-w-6xl mx-auto">
        <h2 class="text-3xl font-bold mb-8">Why beambam</h2>
        <div class="grid md:grid-cols-2 gap-6">
            {#each features as { title, desc }}
                <div class="border border-steel-700 rounded-lg p-6 bg-steel-800/40">
                    <h3 class="text-xl font-semibold text-flame-300 mb-2">{title}</h3>
                    <p class="text-stone-300 leading-relaxed">{@html desc}</p>
                </div>
            {/each}
        </div>
    </div>
</section>

<!-- Quickstart -->
<section class="py-16 px-6 border-t border-steel-700">
    <div class="max-w-6xl mx-auto">
        <h2 class="text-3xl font-bold mb-6">Quickstart</h2>
        <pre><code><span class="text-stone-500"># Install</span>
pip install beambam

<span class="text-stone-500"># Find + register your printer (interactive)</span>
beambam init

<span class="text-stone-500"># Verify everything's healthy</span>
beambam doctor

<span class="text-stone-500"># Preview a print before sending it hot</span>
beambam analyze model.gcode.3mf

<span class="text-stone-500"># Upload + start print with AMS slot 5</span>
beambam print model.gcode.3mf --slot 5

<span class="text-stone-500"># Watch the camera in your terminal</span>
beambam cam watch</code></pre>

        <p class="mt-6 text-stone-400">
            Full reference at <a href="/cli">/cli</a>. Library API + Printer class docs at <a href="/docs">/docs</a>.
        </p>
    </div>
</section>
