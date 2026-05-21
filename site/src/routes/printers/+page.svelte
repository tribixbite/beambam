<script lang="ts">
    type Family = 'X-series' | 'P-series' | 'A-series' | 'H-series' | 'X2D';
    type Row = {
        family: Family;
        model: string;
        code: string;
        signed: 'optional¹' | 'required' | 'required²' | 'required (dual)';
        notes: string;
        primary?: true;
    };

    const rows: Row[] = [
        { family: 'X-series',  model: 'X1',          code: 'BL-P002', signed: 'optional¹',       notes: 'original H2 family' },
        { family: 'X-series',  model: 'X1 Carbon',   code: 'BL-P001', signed: 'optional¹',       notes: 'original H2 family' },
        { family: 'X-series',  model: 'X1E',         code: 'C13',     signed: 'required',        notes: 'enterprise variant' },
        { family: 'P-series',  model: 'P1P',         code: 'C11',     signed: 'required²',       notes: 'no chamber, no AMS lite' },
        { family: 'P-series',  model: 'P1S',         code: 'C12',     signed: 'required²',       notes: 'enclosed P-series' },
        { family: 'P-series',  model: 'P2S',         code: 'N7',      signed: 'required',        notes: 'newer P-series' },
        { family: 'A-series',  model: 'A1',          code: 'N2S',     signed: 'required²',       notes: 'direct drive, AMS lite' },
        { family: 'A-series',  model: 'A1 mini',     code: 'N1',      signed: 'required²',       notes: '180mm cube, AMS lite' },
        { family: 'H-series',  model: 'H2D',         code: 'O1D',     signed: 'required (dual)', notes: 'dual nozzle, multi-AMS' },
        { family: 'H-series',  model: 'H2D Pro',     code: 'O1E',     signed: 'required (dual)', notes: 'enterprise H2D' },
        { family: 'H-series',  model: 'H2S',         code: 'O1S',     signed: 'required',        notes: 'single-nozzle H2' },
        { family: 'H-series',  model: 'H2C',         code: 'O1C2',    signed: 'required',        notes: 'compact H-series' },
        { family: 'X2D',       model: 'X2D',         code: 'N6',      signed: 'required (dual)', notes: 'primary target — analyze developed here', primary: true }
    ];

    const matrix = [
        { key: 'status',    label: 'Status' },
        { key: 'upload',    label: 'Upload' },
        { key: 'print',     label: 'Print' },
        { key: 'ams',       label: 'AMS' },
        { key: 'camera',    label: 'Camera' }
    ];

    function cellFor(row: Row, key: string): string {
        if (key === 'camera') {
            if (row.family === 'X-series' || row.family === 'H-series' || row.family === 'X2D') return 'RTSPS';
            return 'HTTP/MJPEG';
        }
        if (key === 'ams') {
            if (row.family === 'A-series') return '\u2713 lite';
            if (row.family === 'H-series' || row.family === 'X2D') return '\u2713 multi';
            return '\u2713';
        }
        return '\u2713';
    }
</script>

<svelte:head>
    <title>Compatibility — beambam</title>
</svelte:head>

<section>
    <div class="frame py-[var(--space-3xl)]">
        <div class="label mb-[var(--space-md)]">Section 02 &middot; Compatibility matrix</div>
        <h1 class="mb-[var(--space-xl)]" style="font-size: var(--text-2xl);">
            <span style="color: var(--color-accent);">13</span> printers, one bridge.
        </h1>
        <p class="max-w-[65ch] leading-[1.65]" style="color: var(--color-mute);">
            Every Bambu Lab printer that exposes the standard LAN MQTT
            (port 8883, TLS) + FTPS (port 990, implicit TLS) surface. The
            signed-MQTT requirement&nbsp;<sup class="fn">1</sup>&nbsp;is handled
            transparently using the publicly-leaked Bambu Connect cert
            (ID <code>GLOF1000000000-...</code>). No cloud account or
            Bambu-issued token needed for any operation here.
        </p>

        <hr class="hr-dashed" />

        <div class="overflow-x-auto">
            <table class="spec min-w-[64rem]">
                <thead>
                    <tr>
                        <th class="w-[6rem]">Family</th>
                        <th class="w-[7rem]">Model</th>
                        <th class="w-[5rem]">Code</th>
                        <th class="w-[8rem]">Signed MQTT</th>
                        {#each matrix as { label }}
                            <th class="w-[6rem]">{label}</th>
                        {/each}
                        <th>Notes</th>
                    </tr>
                </thead>
                <tbody>
                    {#each rows as row}
                        <tr>
                            <td class="font-[var(--font-mono)] text-[var(--text-xs)] uppercase" style="color: var(--color-mute);">{row.family}</td>
                            <td class="font-[var(--font-display)] text-[var(--text-lg)] leading-none tracking-tight"
                                style="color: {row.primary ? 'var(--color-accent)' : 'var(--color-ink)'};">{row.model}</td>
                            <td class="font-[var(--font-mono)] text-[var(--text-xs)]" style="color: var(--color-mute);">{row.code}</td>
                            <td class="font-[var(--font-mono)] text-[var(--text-xs)]"
                                style="color: {row.signed.includes('dual') ? 'var(--color-accent)' : 'var(--color-ink)'};">{row.signed}</td>
                            {#each matrix as { key }}
                                <td class="font-[var(--font-mono)] text-[var(--text-sm)]" style="color: var(--color-ink);">{cellFor(row, key)}</td>
                            {/each}
                            <td class="text-[var(--text-sm)]" style="color: var(--color-mute);">{row.notes}</td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>

        <div class="mt-[var(--space-2xl)] grid md:grid-cols-2 gap-[var(--space-lg)] text-[var(--text-sm)] leading-[1.6] max-w-[80ch]" style="color: var(--color-mute);">
            <p>
                <sup class="fn">1</sup>&nbsp;X1 / X1C with pre-2025 firmware accept
                unsigned MQTT; the bridge signs anyway (no overhead, forward
                compatible).
            </p>
            <p>
                <sup class="fn">2</sup>&nbsp;P1S / P1P / A1 enforcement varies by
                firmware build; the bridge signs unconditionally.
            </p>
        </div>

        <hr class="hr-dashed" />

        <div class="grid md:grid-cols-12 gap-x-[var(--space-2xl)] gap-y-[var(--space-md)]">
            <div class="md:col-span-3">
                <div class="label">Not listed?</div>
            </div>
            <div class="md:col-span-9 max-w-[55ch] leading-[1.65]" style="color: var(--color-mute);">
                If your model has the LAN-MQTT switch in <em>Settings &rarr; Network</em>
                and lets you set an access code, it almost certainly works.
                <a href="https://github.com/tribixbite/beambam/issues" target="_blank" rel="noopener">Open an issue</a>
                with the <code>X-BBL-Device-Model</code> value from
                <code>beambam status</code> and we'll add it to the table.
            </div>
        </div>
    </div>
</section>
