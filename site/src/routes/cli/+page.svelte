<script lang="ts">
    import CodeBlock from '$lib/CodeBlock.svelte';

    type Cmd = { name: string; desc: string; example?: string; v?: string };

    const sections: { id: string; title: string; description: string; cmds: Cmd[] }[] = [
        {
            id: 'setup',
            title: 'Setup',
            description: 'Get a fresh install to "working" in one command.',
            cmds: [
                { name: 'init',     desc: 'Interactive first-run wizard (discover → code → test → write).', example: 'beambam init --name studio', v: 'v1.2' },
                { name: 'find',     desc: 'SSDP LAN discovery.', example: 'beambam find --timeout 5', v: 'v1.2' },
                { name: 'config',   desc: 'Credentials editor (list / show / add / remove / rename).', example: 'beambam config list --reveal', v: 'v1.2' }
            ]
        },
        {
            id: 'live',
            title: 'Live control',
            description: 'Talk to the printer over signed MQTT.',
            cmds: [
                { name: 'status',        desc: 'Pull live printer state.' },
                { name: 'pause',         desc: 'Pause the current print.' },
                { name: 'resume',        desc: 'Resume a paused print.' },
                { name: 'stop',          desc: 'Abort current print (cannot resume).' },
                { name: 'gcode',         desc: 'Send a raw G-code line.', example: 'beambam gcode "G28 Z"' },
                { name: 'set-temp',      desc: 'Set heater target.', example: 'beambam set-temp bed 60' },
                { name: 'chamber-light', desc: 'Toggle chamber LED.' },
                { name: 'jog',           desc: 'Manual XYZ axis jog.' },
                { name: 'home',          desc: 'Home axes.' },
                { name: 'level',         desc: 'Auto bed-level.' }
            ]
        },
        {
            id: 'ams',
            title: 'AMS',
            description: 'Slot inspection + filament control.',
            cmds: [
                { name: 'ams status',  desc: 'Pretty AMS state — colors, humidity, slots.', v: 'v1.2' },
                { name: 'ams info N',  desc: 'Detail for one tray by global slot 0..15.', v: 'v1.2' },
                { name: 'ams load N',  desc: 'Load filament from slot N.' },
                { name: 'ams unload',  desc: 'Unload current filament.' },
                { name: 'ams dry',     desc: 'Start a drying cycle on an AMS unit.', example: 'beambam ams dry 0 --temp 55 --hours 8', v: 'v1.2' },
                { name: 'ams set N P', desc: 'Push tray_info_idx + temps + color from a flat profile.', example: "beambam ams set 7 './eSUN PLA+.json' --color F98C36", v: 'v1.3' },
                { name: 'ams sync',    desc: 'Batch-push every slot from flat-profiles/ams-sync.json.', example: 'beambam ams sync --dry-run', v: 'v1.3' }
            ]
        },
        {
            id: 'files',
            title: 'Files',
            description: 'Push / pull / list / slice / analyze.',
            cmds: [
                { name: 'upload',    desc: 'Upload .gcode.3mf to printer SD.' },
                { name: 'download',  desc: 'Pull a file off the printer SD via FTPS.', example: "beambam download '/cache/x.3mf'", v: 'v1.2' },
                { name: 'files',     desc: 'List printer SD contents.' },
                { name: 'fetch',     desc: 'Download from MakerWorld / direct URL.' },
                { name: 'slice',     desc: 'Standalone STL slice via BambuStudio CLI.', example: 'beambam slice model.stl -o out.gcode.3mf', v: 'v1.2' },
                { name: 'analyze',   desc: 'Dissect a local .gcode.3mf — flushes, AMS, hints.', example: 'beambam analyze model.gcode.3mf', v: 'v1.2' },
                { name: 'frame',     desc: 'Generate a picture-frame STL with debossed text.', example: 'beambam frame --preset rumi -o rumi.stl', v: 'v1.2' }
            ]
        },
        {
            id: 'printing',
            title: 'Printing',
            description: 'Upload + start, or queue for later.',
            cmds: [
                { name: 'print',         desc: 'Upload + start print with AMS mapping.', example: 'beambam print model.3mf --slot 5' },
                { name: 'slice-print',   desc: 'Slice + upload + start in one shot.' },
                { name: 'simulate',      desc: 'Dry-run the signed MQTT payload (no publish).' },
                { name: 'queue list',    desc: 'Show pending jobs.', v: 'v1.2' },
                { name: 'queue add',     desc: 'Enqueue a .gcode.3mf.', v: 'v1.2' },
                { name: 'queue rm',      desc: 'Delete a job.', v: 'v1.2' },
                { name: 'queue clear',   desc: 'Remove all jobs.', v: 'v1.2' }
            ]
        },
        {
            id: 'diag',
            title: 'Diagnostics',
            description: 'Pre-print sanity + debugging.',
            cmds: [
                { name: 'doctor',   desc: 'Full health check — AMS, HMS, sensors, wifi.', v: 'v1.2' },
                { name: 'health',   desc: 'Connectivity + MQTT smoke test.' },
                { name: 'mqtt sub', desc: 'Stream the printer reply topic.', v: 'v1.2' },
                { name: 'mqtt pub', desc: 'Sign + publish arbitrary JSON.', example: 'beambam mqtt pub \'{"print":{"command":"pause"}}\'', v: 'v1.2' }
            ]
        },
        {
            id: 'camera',
            title: 'Camera',
            description: 'Live view + snapshots.',
            cmds: [
                { name: 'cam watch', desc: 'Live terminal viewer (kitty / iTerm2 / blocks).', v: 'v1.2' },
                { name: 'cam snap',  desc: 'One-shot snapshot save.', v: 'v1.2' },
                { name: 'camera',    desc: 'Start RTSP camera proxy daemon.' },
                { name: 'webrtc',    desc: 'WebRTC gateway for browser viewing.' }
            ]
        },
        {
            id: 'cloud',
            title: 'Cloud (optional)',
            description: 'Bambu Cloud + MakerWorld queries.',
            cmds: [
                { name: 'cloud-login',         desc: 'Auth to Bambu Cloud.' },
                { name: 'cloud-status',        desc: 'Check current session.' },
                { name: 'whoami',              desc: 'Logged-in user identity.', v: 'v1.2' },
                { name: 'history',             desc: 'Recent cloud print history.', v: 'v1.2' },
                { name: 'cloud-fetch --info',  desc: 'MakerWorld design metadata.', example: 'beambam cloud-fetch --info 1501027', v: 'v1.2' },
                { name: 'cloud-print',         desc: 'Cloud-side print start.' }
            ]
        },
        {
            id: 'daemon',
            title: 'Daemon',
            description: 'Long-running services.',
            cmds: [
                { name: 'daemon',     desc: 'HTTP + SSE + Prometheus + HA + queue.', example: 'beambam daemon --http :8765 --queue --timelapse' },
                { name: 'serve',      desc: 'Unix-socket RPC for libbambu_networking.so.' },
                { name: 'ha-publish', desc: 'One-shot HA MQTT discovery push.' },
                { name: 'watch',      desc: 'Tail state updates.' },
                { name: 'timelapse',  desc: 'Start/stop on-printer timelapse.' }
            ]
        }
    ];

    const total = sections.reduce((n, s) => n + s.cmds.length, 0);
</script>

<svelte:head>
    <title>Command index — beambam</title>
</svelte:head>

<section>
    <div class="frame py-[var(--space-3xl)] grid gap-[var(--space-2xl)] md:grid-cols-12 md:gap-x-[var(--space-2xl)]">

        <!-- HEADER COLUMN — also acts as sidebar nav on desktop -->
        <div class="md:col-span-4">
            <div class="label mb-[var(--space-md)]">03 · Command index</div>
            <!-- Two-line stacked heading prevents "subcommands" overflowing
                 the narrow sidebar column at md (where the column is ~14rem
                 and the word at text-2xl exceeds it). -->
            <h1 class="mb-[var(--space-lg)] leading-[0.95]" style="font-size: clamp(2.25rem, 1.6rem + 2.5vw, 3rem); word-break: keep-all; overflow-wrap: break-word;">
                <span style="color: var(--color-accent); display: block;">{total}</span>
                <span style="display: block;">subcommands.</span>
            </h1>
            <p class="leading-[1.65] max-w-[42ch] text-[var(--text-sm)] sm:text-[var(--text-base)]" style="color: var(--color-mute);">
                Each verb is also importable under <code>beambam.</code>. Every
                command accepts <code>--ip</code> / <code>--code</code> /
                <code>--serial</code> / <code>--printer&nbsp;NAME</code> to override
                the default printer section.
            </p>

            <!-- Desktop sticky TOC. Hidden on mobile — sections scroll naturally. -->
            <nav class="mt-[var(--space-xl)] hidden md:block sticky" style="top: 72px;">
                <div class="label mb-[var(--space-sm)]">In this index</div>
                <ol class="space-y-[var(--space-xs)] font-[var(--font-mono)] text-[var(--text-xs)] uppercase tracking-widest">
                    {#each sections as { id, title, cmds }, i}
                        <li>
                            <a href={'#' + id}
                               class="no-underline flex items-baseline gap-[var(--space-md)] py-[2px]"
                               style="text-decoration: none; color: var(--color-mute);">
                                <span style="color: var(--color-mute-2);">{String(i + 1).padStart(2, '0')}</span>
                                <span>{title}</span>
                                <span class="ml-auto" style="color: var(--color-mute-2);">{cmds.length}</span>
                            </a>
                        </li>
                    {/each}
                </ol>
            </nav>

            <!-- Mobile TOC chip strip — horizontal scroll, easy thumb-tap -->
            <nav class="md:hidden mt-[var(--space-xl)] -mx-[var(--space-xl)] overflow-x-auto" style="scrollbar-width: none;">
                <div class="flex gap-[var(--space-sm)] px-[var(--space-xl)] pb-[var(--space-sm)]" style="white-space: nowrap;">
                    {#each sections as { id, title }, i}
                        <a href={'#' + id}
                           class="badge no-underline shrink-0"
                           style="text-decoration: none; color: var(--color-mute);">
                            <span style="color: var(--color-mute-2);">{String(i + 1).padStart(2, '0')}</span>
                            &nbsp;{title}
                        </a>
                    {/each}
                </div>
            </nav>
        </div>

        <!-- COMMAND LIST BODY -->
        <div class="md:col-span-8 min-w-0 space-y-[var(--space-4xl)]">
            {#each sections as { id, title, description, cmds }, i}
                <section id={id} class="scroll-mt-[80px]">
                    <header class="pb-[var(--space-md)] mb-[var(--space-lg)] flex items-baseline gap-[var(--space-md)] sm:gap-[var(--space-lg)] flex-wrap" style="border-bottom: 1px solid var(--color-hair);">
                        <span class="font-[var(--font-mono)] text-[var(--text-xs)]" style="color: var(--color-mute-2);">{String(i + 1).padStart(2, '0')}</span>
                        <h2 class="text-[var(--text-xl)] leading-none m-0" style="color: var(--color-ink);">{title}</h2>
                        <span class="ml-auto label">{cmds.length} commands</span>
                    </header>
                    <p class="mb-[var(--space-xl)] text-[var(--text-sm)] max-w-[55ch]" style="color: var(--color-mute);">{description}</p>

                    <dl class="grid gap-y-[var(--space-md)]">
                        {#each cmds as { name, desc, example, v }}
                            <div class="grid md:grid-cols-12 gap-x-[var(--space-lg)] gap-y-[var(--space-xs)] py-[var(--space-md)] last:border-0"
                                 style="border-bottom: 1px dashed var(--color-hair);">
                                <dt class="md:col-span-4 font-[var(--font-mono)] text-[var(--text-sm)] flex items-baseline gap-[var(--space-sm)] flex-wrap" style="color: var(--color-ink); word-break: break-word;">
                                    <span>beambam&nbsp;{name}</span>
                                    {#if v}
                                        <span class="text-[10px] uppercase tracking-widest font-[var(--font-mono)]" style="color: var(--color-accent-dim);">{v}</span>
                                    {/if}
                                </dt>
                                <dd class="md:col-span-8 text-[var(--text-sm)] leading-[1.6] min-w-0" style="color: var(--color-mute);">
                                    {desc}
                                    {#if example}
                                        <div class="mt-[var(--space-xs)]">
                                            <CodeBlock code={example} variant="line" ariaLabel="Copy example" />
                                        </div>
                                    {/if}
                                </dd>
                            </div>
                        {/each}
                    </dl>
                </section>
            {/each}

            <hr class="hr-dashed" style="margin: 0;" />
            <p class="text-[var(--text-sm)] leading-[1.6]" style="color: var(--color-mute);">
                Full per-command help: <code>beambam &lt;cmd&gt; --help</code>.
                Deep-dive docs: <a href="/docs">/docs</a>.
                v1.2 tag marks commands new in the current release.
            </p>
        </div>
    </div>
</section>
