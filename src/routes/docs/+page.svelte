<script lang="ts">
    const sections = [
        {
            title: 'Quickstart',
            items: [
                ['Install', '`pip install beambam` (any Linux / macOS / WSL)'],
                ['First run', '`beambam init` — discover + write credentials + verify'],
                ['Verify', '`beambam status` and `beambam doctor`'],
                ['Print', '`beambam print model.gcode.3mf --slot 5`']
            ]
        },
        {
            title: 'Library API',
            items: [
                ['Printer facade', '`from beambam import Printer, Creds`'],
                ['One-shot state', '`Printer().state()`'],
                ['Lazy MQTT', 'context manager — `with Printer(creds) as p: …`'],
                ['Schemas', '`from beambam.schemas import PrintState, AmsBus`'],
                ['Print analyzer', '`from beambam.analyze import analyze_3mf`']
            ]
        },
        {
            title: 'Integrations',
            items: [
                ['Home Assistant', '~69 entities via MQTT discovery — sensors for temps, AMS slots + humidity warnings, buttons for pause/resume/stop'],
                ['MCP server', '25 tools — runs as `beambam-mcp` stdio for Claude/Cursor/Continue'],
                ['Web UI', '`beambam daemon --http :8765` — mobile-friendly, multi-printer queue, timelapse browser'],
                ['Prometheus', 'daemon exposes /metrics with mqtt_connects_total etc.'],
                ['WebRTC', 'chamber camera at <100ms latency via /cam.webrtc.html']
            ]
        },
        {
            title: 'Concepts',
            items: [
                ['Signed MQTT', 'Jan-2025+ firmware requires RSA-SHA256 on every command. beambam signs transparently using the publicly-leaked Bambu Connect cert.'],
                ['Credentials', '~/.x2d/credentials — INI with `[printer]` (default) or `[printer:NAME]` sections. Multi-printer setups via `--printer NAME` flag or `X2D_PRINTER` env.'],
                ['Print analyzer phases', '`beambam analyze` groups the print into contiguous layer ranges by which filaments are active, then counts real flushes (not nozzle-only swaps) and per-phase purge volume in mm + grams.'],
                ['AMS humidity', 'Levels 0–4 (4 = wet). Sensors fire HA `binary_sensor.ams_unit{N}_humidity_warn` at level ≥3.']
            ]
        }
    ];
</script>

<svelte:head>
    <title>Docs — beambam</title>
</svelte:head>

<section class="py-12 px-6">
    <div class="max-w-4xl mx-auto">
        <h1 class="text-4xl font-bold mb-2">Documentation</h1>
        <p class="text-stone-400 mb-12">
            The full deep-dives live in <a href="https://github.com/tribixbite/beambam/tree/main/docs">docs/</a> in the repo (28 markdown files). Here's the high-level map.
        </p>

        <div class="space-y-10">
            {#each sections as section}
                <section>
                    <h2 class="text-2xl font-semibold text-flame-300 mb-4">{section.title}</h2>
                    <dl class="space-y-3">
                        {#each section.items as [name, desc]}
                            <div class="border-l-2 border-steel-600 pl-4">
                                <dt class="font-semibold text-stone-100">{name}</dt>
                                <dd class="text-sm text-stone-300 mt-1">{@html desc.replace(/`([^`]+)`/g, '<code>$1</code>')}</dd>
                            </div>
                        {/each}
                    </dl>
                </section>
            {/each}
        </div>

        <p class="mt-12 text-sm text-stone-500">
            Missing something? <a href="https://github.com/tribixbite/beambam/issues">Open an issue</a> or scan the
            <a href="https://github.com/tribixbite/beambam/tree/main/docs">docs/ folder</a> directly.
        </p>
    </div>
</section>
