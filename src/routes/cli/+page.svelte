<script lang="ts">
    type Cmd = { name: string; desc: string; example?: string };

    const groups: { title: string; description: string; cmds: Cmd[] }[] = [
        {
            title: 'Setup',
            description: 'Get a fresh install to "working" in one command.',
            cmds: [
                { name: 'init',     desc: 'Interactive first-run wizard (discover → code → test → write).',
                  example: 'beambam init --name studio' },
                { name: 'find',     desc: 'SSDP LAN discovery.',
                  example: 'beambam find --timeout 5' },
                { name: 'config',   desc: 'Credentials editor (list / show / add / remove / rename).',
                  example: 'beambam config list --reveal' }
            ]
        },
        {
            title: 'Live control',
            description: 'Talk to the printer over signed MQTT.',
            cmds: [
                { name: 'status',     desc: 'Pull live printer state.' },
                { name: 'pause',      desc: 'Pause the current print.' },
                { name: 'resume',     desc: 'Resume a paused print.' },
                { name: 'stop',       desc: 'Abort current print (cannot resume).' },
                { name: 'gcode',      desc: 'Send a raw G-code line.',
                  example: 'beambam gcode "G28 Z"' },
                { name: 'set-temp',   desc: 'Set heater target.',
                  example: 'beambam set-temp bed 60' },
                { name: 'chamber-light', desc: 'Toggle chamber LED.' },
                { name: 'jog',        desc: 'Manual XYZ axis jog.' },
                { name: 'home',       desc: 'Home axes.' },
                { name: 'level',      desc: 'Auto bed-level.' }
            ]
        },
        {
            title: 'AMS',
            description: 'Slot inspection + filament control.',
            cmds: [
                { name: 'ams status',  desc: 'Pretty AMS state — colors, humidity, slots.' },
                { name: 'ams info N',  desc: 'Detail for one tray by global slot 0..15.' },
                { name: 'ams load N',  desc: 'Load filament from slot N.' },
                { name: 'ams unload',  desc: 'Unload current filament.' },
                { name: 'ams dry',     desc: 'Start a drying cycle on an AMS unit.',
                  example: 'beambam ams dry 0 --temp 55 --hours 8' }
            ]
        },
        {
            title: 'Files',
            description: 'Push / pull / list / slice / analyze.',
            cmds: [
                { name: 'upload',    desc: 'Upload .gcode.3mf to printer SD.' },
                { name: 'download',  desc: 'Pull a file off the printer SD via FTPS.',
                  example: "beambam download '/cache/x.3mf'" },
                { name: 'files',     desc: 'List printer SD contents.' },
                { name: 'fetch',     desc: 'Download from MakerWorld / direct URL.' },
                { name: 'slice',     desc: 'Standalone STL slice via BambuStudio CLI.',
                  example: 'beambam slice model.stl -o out.gcode.3mf' },
                { name: 'analyze',   desc: 'Dissect a local .gcode.3mf — flushes, AMS, hints.',
                  example: 'beambam analyze model.gcode.3mf' },
                { name: 'frame',     desc: 'Generate a picture-frame STL with debossed text.',
                  example: 'beambam frame --preset rumi -o rumi.stl' }
            ]
        },
        {
            title: 'Printing',
            description: 'Upload + start, or queue for later.',
            cmds: [
                { name: 'print',         desc: 'Upload + start print with AMS mapping.',
                  example: 'beambam print model.3mf --slot 5' },
                { name: 'slice-print',   desc: 'Slice + upload + start in one shot.' },
                { name: 'simulate',      desc: 'Dry-run the signed MQTT payload (no publish).' },
                { name: 'queue list',    desc: 'Show pending jobs.' },
                { name: 'queue add',     desc: 'Enqueue a .gcode.3mf.' },
                { name: 'queue rm',      desc: 'Delete a job.' },
                { name: 'queue clear',   desc: 'Remove all jobs.' }
            ]
        },
        {
            title: 'Diagnostics',
            description: 'Pre-print sanity + debugging.',
            cmds: [
                { name: 'doctor',  desc: 'Full health check — AMS, HMS, sensors, wifi.' },
                { name: 'health',  desc: 'Connectivity + MQTT smoke test.' },
                { name: 'mqtt sub', desc: 'Stream the printer reply topic.' },
                { name: 'mqtt pub', desc: 'Sign + publish arbitrary JSON.',
                  example: 'beambam mqtt pub \'{"print":{"command":"pause"}}\'' }
            ]
        },
        {
            title: 'Camera',
            description: 'Live view + snapshots.',
            cmds: [
                { name: 'cam watch', desc: 'Live terminal viewer (kitty / iTerm2 / blocks).' },
                { name: 'cam snap',  desc: 'One-shot snapshot save.' },
                { name: 'camera',    desc: 'Start RTSP camera proxy daemon.' },
                { name: 'webrtc',    desc: 'WebRTC gateway for browser viewing.' }
            ]
        },
        {
            title: 'Cloud (optional)',
            description: 'Bambu Cloud + MakerWorld queries.',
            cmds: [
                { name: 'cloud-login',       desc: 'Auth to Bambu Cloud.' },
                { name: 'cloud-status',      desc: 'Check current session.' },
                { name: 'whoami',            desc: 'Logged-in user identity.' },
                { name: 'history',           desc: 'Recent cloud print history.' },
                { name: 'cloud-fetch --info', desc: 'MakerWorld design metadata.',
                  example: 'beambam cloud-fetch --info 1501027' },
                { name: 'cloud-print',       desc: 'Cloud-side print start.' }
            ]
        },
        {
            title: 'Daemon',
            description: 'Long-running services.',
            cmds: [
                { name: 'daemon',  desc: 'HTTP + SSE + Prometheus + HA + queue.',
                  example: 'beambam daemon --http :8765 --queue --timelapse' },
                { name: 'serve',   desc: 'Unix-socket RPC for libbambu_networking.so.' },
                { name: 'ha-publish', desc: 'One-shot HA MQTT discovery push.' },
                { name: 'watch',   desc: 'Tail state updates.' },
                { name: 'timelapse', desc: 'Start/stop on-printer timelapse.' }
            ]
        }
    ];
</script>

<svelte:head>
    <title>CLI reference — beambam</title>
</svelte:head>

<section class="py-12 px-6">
    <div class="max-w-6xl mx-auto">
        <h1 class="text-4xl font-bold mb-2">CLI reference</h1>
        <p class="text-stone-400 mb-12">28 subcommands grouped by use. Every command takes <code>--ip</code> / <code>--code</code> / <code>--serial</code> / <code>--printer NAME</code> to override the default printer.</p>

        <div class="space-y-12">
            {#each groups as group}
                <section>
                    <h2 class="text-2xl font-semibold text-flame-300 mb-1">{group.title}</h2>
                    <p class="text-sm text-stone-500 mb-4">{group.description}</p>
                    <div class="grid sm:grid-cols-2 gap-3">
                        {#each group.cmds as { name, desc, example }}
                            <div class="border border-steel-700 rounded p-4 bg-steel-800/40">
                                <code class="text-flame-300 text-sm font-semibold block">beambam {name}</code>
                                <p class="text-sm text-stone-300 mt-2">{desc}</p>
                                {#if example}
                                    <pre class="mt-2 text-xs"><code>{example}</code></pre>
                                {/if}
                            </div>
                        {/each}
                    </div>
                </section>
            {/each}
        </div>

        <p class="mt-12 text-sm text-stone-500">
            Run <code>beambam &lt;cmd&gt; --help</code> for the full flag list of any subcommand.
        </p>
    </div>
</section>
