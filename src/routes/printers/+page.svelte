<script lang="ts">
    type Row = {
        model: string;
        code: string;
        signed: string;
        status: '✅' | '⚠️' | '❌';
        camera: string;
        ams: string;
        notes: string;
    };

    const rows: Row[] = [
        { model: 'X1 / X1C',     code: 'BL-P002 / BL-P001', signed: 'optional¹', status: '✅', camera: 'RTSPS',     ams: '✅', notes: 'original H2 family' },
        { model: 'X1E',          code: 'C13',               signed: 'required',  status: '✅', camera: 'RTSPS',     ams: '✅', notes: 'enterprise variant' },
        { model: 'P1P / P1S',    code: 'C11 / C12',         signed: 'required²', status: '✅', camera: 'HTTP/MJPEG', ams: '✅', notes: 'P-series' },
        { model: 'P2S',          code: 'N7',                signed: 'required',  status: '✅', camera: 'HTTP/MJPEG', ams: '✅', notes: 'newer P-series' },
        { model: 'A1 / A1 mini', code: 'N2S / N1',          signed: 'required²', status: '✅', camera: 'HTTP/MJPEG', ams: '✅ (AMS lite)', notes: 'direct drive' },
        { model: 'H2D / H2D Pro', code: 'O1D / O1E',        signed: 'required',  status: '✅', camera: 'RTSPS',     ams: '✅ multi-AMS', notes: 'dual nozzle' },
        { model: 'H2S / H2C',    code: 'O1S / O1C2',        signed: 'required',  status: '✅', camera: 'RTSPS',     ams: '✅', notes: '' },
        { model: 'X2D',          code: 'N6',                signed: 'required',  status: '✅', camera: 'RTSPS',     ams: '✅ multi-AMS + dynamic map', notes: 'primary target — analyze developed here' }
    ];
</script>

<svelte:head>
    <title>Printer compatibility — beambam</title>
</svelte:head>

<section class="py-16 px-6">
    <div class="max-w-6xl mx-auto">
        <h1 class="text-4xl font-bold mb-4">Printer compatibility</h1>
        <p class="text-stone-400 max-w-3xl mb-8 leading-relaxed">
            Every Bambu Lab printer that exposes LAN MQTT + FTPS. Signed-MQTT (Jan-2025+ firmware, always-on for X2D / H2D-family) is handled with the publicly-leaked Bambu Connect cert — no cloud account or token needed.
        </p>

        <div class="overflow-x-auto rounded-lg border border-steel-700">
            <table class="w-full text-sm">
                <thead class="bg-steel-800 text-stone-400 uppercase tracking-wider text-xs">
                    <tr>
                        <th class="px-4 py-3 text-left">Model</th>
                        <th class="px-4 py-3 text-left">Bambu code</th>
                        <th class="px-4 py-3 text-left">Signed MQTT</th>
                        <th class="px-4 py-3 text-center">Status</th>
                        <th class="px-4 py-3 text-left">Camera</th>
                        <th class="px-4 py-3 text-left">AMS</th>
                        <th class="px-4 py-3 text-left">Notes</th>
                    </tr>
                </thead>
                <tbody class="divide-y divide-steel-700">
                    {#each rows as r}
                        <tr class="hover:bg-steel-800/60 transition">
                            <td class="px-4 py-3 font-medium text-flame-300">{r.model}</td>
                            <td class="px-4 py-3 text-stone-400 font-mono text-xs">{r.code}</td>
                            <td class="px-4 py-3 text-stone-300">{r.signed}</td>
                            <td class="px-4 py-3 text-center text-lg">{r.status}</td>
                            <td class="px-4 py-3 text-stone-400">{r.camera}</td>
                            <td class="px-4 py-3 text-stone-300">{r.ams}</td>
                            <td class="px-4 py-3 text-stone-400 italic">{r.notes}</td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>

        <p class="mt-6 text-sm text-stone-500 max-w-3xl">
            ¹ X1 / X1C with pre-2025 firmware accept unsigned MQTT; bridge signs anyway (zero overhead, forward-compat).<br/>
            ² P1S / P1P / A1 enforcement varies by firmware; bridge signs regardless.
        </p>

        <p class="mt-4 text-sm text-stone-400">
            If your model isn't listed and it has the LAN MQTT switch in Settings → Network, it almost certainly works —
            <a href="https://github.com/tribixbite/beambam/issues">open an issue</a> with the <code>X-BBL-Device-Model</code> header from <code>beambam status</code>.
        </p>
    </div>
</section>
