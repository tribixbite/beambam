<script lang="ts">
    type Doc = { id: string; name: string; desc: string };

    const groups: { title: string; intro: string; docs: Doc[] }[] = [
        {
            title: 'Quickstart',
            intro: 'Get from a fresh `pip install` to your first network-attached print in under 90 seconds.',
            docs: [
                { id: 'QUICKSTART', name: 'QUICKSTART.md', desc: 'Install, configure, first print.' },
                { id: 'init',       name: 'CLI: beambam init', desc: 'Interactive first-run wizard — discover, prompt for code, test, write credentials.' }
            ]
        },
        {
            title: 'Library API',
            intro: 'beambam is importable from Python. The high-level Printer class wraps the signed-MQTT and FTPS surfaces.',
            docs: [
                { id: 'lib-printer',  name: 'beambam.Printer',       desc: 'High-level facade. Lazy MQTT + Cloud. Context-manager protocol. 18 methods (state, start_print, pause, gcode, set_temp, ams_*, jog, home, upload, download, …).' },
                { id: 'lib-config',   name: 'beambam.config.Creds',  desc: 'Printer credentials. resolve(args) honours $X2D_IP/$X2D_CODE/$X2D_SERIAL/$X2D_PRINTER + ~/.x2d/credentials [printer:NAME] sections.' },
                { id: 'lib-mqtt',     name: 'beambam.mqtt',          desc: 'sign_payload + BAMBU_CERT_ID + X2DClient. Bambu Connect cert is loaded lazily on first sign.' },
                { id: 'lib-ftps',     name: 'beambam.ftps',          desc: 'upload_file / download_file / list_files. Implicit TLS, session-reuse on PASV, INVALID_ALERT workaround for mid-print downloads.' },
                { id: 'lib-schemas',  name: 'beambam.schemas',       desc: 'TypedDicts for PushAllReport / PrintState / AmsBus / AmsUnit / AmsTray / StartPrintCommand. Mypy-friendly.' },
                { id: 'lib-analyze',  name: 'beambam.analyze',       desc: 'Print-plan dissector. Parse a .gcode.3mf into phases, count real flushes, project AMS-tray requirements.' }
            ]
        },
        {
            title: 'Integrations',
            intro: 'beambam ships one daemon that publishes to every common downstream surface.',
            docs: [
                { id: 'HA',          name: 'docs/HA.md',           desc: 'Home Assistant MQTT auto-discovery. ~69 entities including per-tray AMS color, humidity warnings, queue depth, HMS active count.' },
                { id: 'HA_SETUP',    name: 'docs/HA_SETUP.md',     desc: 'Step-by-step HA install + dashboard YAML.' },
                { id: 'MCP',         name: 'docs/MCP.md',          desc: 'Model Context Protocol stdio server. 25 tools for Claude Desktop, Cursor, Continue.' },
                { id: 'WEBRTC',      name: 'docs/WEBRTC.md',       desc: 'Chamber-camera streaming under 100ms latency.' },
                { id: 'WEB_UI',      name: 'docs/WEB_UI.md',       desc: 'Mobile-friendly multi-printer web UI.' },
                { id: 'QUEUE',       name: 'docs/QUEUE.md',        desc: 'Persistent print queue with auto-dispatch and AMS validation.' },
                { id: 'TIMELAPSE',   name: 'docs/TIMELAPSE.md',    desc: 'Auto-recorded timelapses, ffmpeg stitched.' }
            ]
        },
        {
            title: 'Protocol notes',
            intro: 'Wire-level reverse-engineering notes. Read these if you\u2019re extending beambam or building something compatible.',
            docs: [
                { id: 'LOCAL_CONTROL_PATHS',   name: 'docs/LOCAL_CONTROL_PATHS.md',    desc: 'LAN MQTT, FTPS, RTSPS, LVL-Local — every protocol the printer speaks on the local network.' },
                { id: 'SIGNED_VS_UNSIGNED',    name: 'docs/SIGNED_VS_UNSIGNED.md',     desc: 'Signed vs unsigned MQTT truth table per firmware version. Includes the leaked-cert background.' },
                { id: 'DART_HEAP_KEY',         name: 'runtime/handy_extract/DART_HEAP_KEY_EXTRACTION.md', desc: 'How beambam recovers the per-installation signing key from the Bambu Handy Dart heap — scanning for a 128-byte window that divides the known modulus (a prime factor). No Frida, no hooking.' },
                { id: 'COMPARISON',            name: 'docs/COMPARISON.md',            desc: 'How beambam compares to ha-bambulab, bambu-mcp, bambulabs_api, OrcaSlicer and others — and why it is the only one that starts a print over pure LAN on signed firmware with no Developer Mode and no live cloud at runtime (the one-time setup extracts a Bambu-issued key from a signed-in Handy).' },
                { id: 'X2D_RUNTIME_PIPELINE',  name: 'docs/X2D_RUNTIME_PIPELINE.md',   desc: 'How a print command flows from CLI through the bridge to the printer\u2019s firmware.' },
                { id: 'CLOUD_BRIDGE',          name: 'docs/CLOUD_BRIDGE.md',           desc: 'Optional Bambu Cloud bridge — cloud-print, cloud-state, cloud-pause.' }
            ]
        },
        {
            title: 'Concepts',
            intro: 'The things you\u2019ll learn while operating beambam in the field.',
            docs: [
                { id: 'c-signed',        name: 'Signed MQTT',
                  desc: 'Jan-2025+ firmware rejects every MQTT command lacking a header.sign_string that verifies against a recognised RSA cert. The leaked Bambu Connect cert covers older firmware; authorization-control firmware (X2D/H2D, refreshed P1/X1) requires a signing cert whose CN matches the printer serial, which beambam recovers per-installation from a Bambu Handy app (beambam key). Your access code stays on your LAN.' },
                { id: 'c-credentials',   name: 'Credentials INI',
                  desc: '~/.x2d/credentials is an INI file with [printer] (default) or [printer:NAME] sections. Multi-printer setups select via --printer NAME or X2D_PRINTER env var.' },
                { id: 'c-analyzer',      name: 'Phases + flush analysis',
                  desc: 'beambam analyze groups the print into contiguous layer ranges by which filaments are active. A tri-color phase against two nozzles forces flushes; the report flags it before you commit to printing.' },
                { id: 'c-humidity',      name: 'AMS humidity scale',
                  desc: 'Bambu firmware reports humidity as a 0–4 level. beambam doctor warns at ≥3 and HA fires binary_sensor.ams_unit{N}_humidity_warn at the same threshold.' }
            ]
        }
    ];
</script>

<svelte:head>
    <title>Reference — beambam</title>
</svelte:head>

<section>
    <div class="frame py-[var(--space-3xl)] grid gap-[var(--space-2xl)] md:grid-cols-12 md:gap-x-[var(--space-2xl)]">
        <div class="md:col-span-4">
            <div class="label mb-[var(--space-md)]">04 · Reference</div>
            <!-- Match /cli sizing — leaves room in a 4-col sidebar without
                 the word overflowing. -->
            <h1 class="mb-[var(--space-lg)] leading-[0.95]" style="font-size: clamp(2.25rem, 1.6rem + 2.5vw, 3rem);">
                Read the<br />manual.
            </h1>
            <p class="leading-[1.65] max-w-[40ch] text-[var(--text-sm)] sm:text-[var(--text-base)]" style="color: var(--color-mute);">
                Long-form deep-dives live under
                <a href="https://github.com/tribixbite/beambam/tree/main/docs" target="_blank" rel="noopener">docs/</a>
                in the repo (28 markdown files). This page indexes them by
                topic; the reading happens on GitHub or in your editor.
            </p>
            <p class="mt-[var(--space-md)] leading-[1.65] max-w-[40ch] text-[var(--text-sm)]" style="color: var(--color-mute);">
                CLI flag detail: <a href="/cli">/cli</a>.
                Per-version changes: <a href="https://github.com/tribixbite/beambam/blob/main/CHANGELOG.md" target="_blank" rel="noopener">CHANGELOG.md</a>.
                What's next: <a href="https://github.com/tribixbite/beambam/blob/main/ROADMAP.md" target="_blank" rel="noopener">ROADMAP.md</a>.
            </p>
        </div>

        <div class="md:col-span-8 min-w-0 space-y-[var(--space-4xl)]">
            {#each groups as { title, intro, docs }, gi}
                <section>
                    <header class="pb-[var(--space-md)] mb-[var(--space-lg)] flex items-baseline gap-[var(--space-md)] flex-wrap" style="border-bottom: 1px solid var(--color-hair);">
                        <span class="font-[var(--font-mono)] text-[var(--text-xs)]" style="color: var(--color-mute-2);">{String(gi + 1).padStart(2, '0')}</span>
                        <h2 class="text-[var(--text-xl)] leading-none m-0" style="color: var(--color-ink);">{title}</h2>
                        <span class="ml-auto label">{docs.length} entries</span>
                    </header>
                    <p class="text-[var(--text-sm)] mb-[var(--space-xl)] max-w-[55ch] leading-[1.65]" style="color: var(--color-mute);">{intro}</p>

                    <dl class="grid gap-y-[var(--space-md)]">
                        {#each docs as { name, desc }}
                            <div class="grid md:grid-cols-12 gap-x-[var(--space-lg)] gap-y-[var(--space-xs)] py-[var(--space-md)] last:border-0"
                                 style="border-bottom: 1px dashed var(--color-hair);">
                                <dt class="md:col-span-4 font-[var(--font-mono)] text-[var(--text-sm)]" style="color: var(--color-ink); word-break: break-word;">{name}</dt>
                                <dd class="md:col-span-8 text-[var(--text-sm)] leading-[1.6] min-w-0" style="color: var(--color-mute);">{desc}</dd>
                            </div>
                        {/each}
                    </dl>
                </section>
            {/each}
        </div>
    </div>
</section>
