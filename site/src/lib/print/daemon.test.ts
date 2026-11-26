import { describe, expect, test } from 'bun:test';
import { Daemon, DaemonError, type FetchLike, headerName, normaliseUrl, startCommand, versionAtLeast } from './daemon';

type Route = (init: RequestInit) => [number, unknown] | 'refuse';

/** A fake fetch serving `routes` by path; records each call. */
function fake(routes: Record<string, Route>, calls: [string, RequestInit][] = []): FetchLike {
    return (async (input: RequestInfo | URL, init: RequestInit = {}) => {
        const url = new URL(String(input));
        calls.push([url.pathname, init]);
        const r = routes[url.pathname];
        if (!r) return new Response('{"error":"not found"}', { status: 404 });
        const got = r(init);
        if (got === 'refuse') throw new TypeError('Failed to fetch');
        return new Response(JSON.stringify(got[1]), { status: got[0], headers: { 'Content-Type': 'application/json' } });
    });
}

const ORIGIN = 'https://beambam.boo';

describe('addresses', () => {
    test('normalise and start command', () => {
        expect(normaliseUrl('127.0.0.1:8765/')).toBe('http://127.0.0.1:8765');
        expect(normaliseUrl(' http://pi.local:9000 ')).toBe('http://pi.local:9000');
        expect(() => normaliseUrl('')).toThrow();
        expect(startCommand(ORIGIN)).toBe('beambam daemon --http 127.0.0.1:8765 --cors-origin https://beambam.boo');
        expect(headerName('Jolteon é toy.3mf')).toBe('Jolteon_toy.3mf');
        expect(headerName('###')).toBe('model.3mf');
        expect(versionAtLeast('1.7.0+source', '1.7.0')).toBe(true);
        expect(versionAtLeast('1.6.9', '1.7.0')).toBe(false);
    });
});

describe('Daemon', () => {
    test('health reads a 503 as a silent printer, not a failure', async () => {
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/healthz': () => [503, { healthy: false, version: '1.7.0', capabilities: ['slice-options'], last_message_age_s: 99 }]
        }) });
        expect(await d.health()).toEqual({ healthy: false, version: '1.7.0', capabilities: ['slice-options'], lastMessageAge: 99, printerSilent: true, camera: null });
    });

    test('the camera: a fetch with the token, and why it does not show in beambam\'s words', async () => {
        const calls: [string, RequestInit][] = [];
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, token: 'sekrit', fetchImpl: fake({
            '/healthz': () => [200, { healthy: true, version: '1.9.0', capabilities: ['camera'], camera: true }],
            '/camera.mjpeg': () => [503, 'camera proxy unreachable at http://127.0.0.1:8766/cam.mjpeg; start it with `beambam cam start`']
        }, calls) });
        expect((await d.health()).camera).toBe(true);
        const ctl = new AbortController();
        const err = await d.camera(ctl.signal).catch((e: DaemonError) => e);
        expect(err).toBeInstanceOf(DaemonError);
        expect((err as DaemonError).message).toContain('beambam cam start');
        expect((calls.at(-1)![1].headers as Record<string, string>).Authorization).toBe('Bearer sekrit');
        const ok = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: (async () =>
            new Response('--frame', { status: 200, headers: { 'Content-Type': 'multipart/x-mixed-replace; boundary=frame' } })) as FetchLike });
        expect((await ok.camera(ctl.signal)).status).toBe(200);
        const refused = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/camera.mjpeg': () => [403, { error: 'the camera is shown only to the pages this daemon allows (--cors-origin)' }] }) });
        expect((await refused.camera(ctl.signal).catch((e: Error) => e.message))).toContain('only to the pages this daemon allows');
    });

    test('a daemon on the LAN: its start command has a token, and every call carries it', async () => {
        expect(startCommand(ORIGIN, 'http://192.168.1.20:8765')).toBe(
            'beambam daemon --http 0.0.0.0:8765 --auth-token "$(openssl rand -hex 16)" --cors-origin https://beambam.boo');
        const calls: [string, RequestInit][] = [];
        const d = new Daemon('http://192.168.1.20:8765', { origin: ORIGIN, token: ' t0k ', fetchImpl: fake({
            '/print/plan': () => [200, { filaments: [], errors: [], warnings: [], notes: [], ok: true }] }, calls) });
        await d.request('/print/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const h = calls[0][1].headers as Record<string, string>;
        expect(h.Authorization).toBe('Bearer t0k');
        expect(h['Content-Type']).toBe('application/json');                // the call's own headers stay
        expect((calls[0][1] as { targetAddressSpace?: string }).targetAddressSpace).toBe('local');
        const missing = new Daemon('http://192.168.1.20:8765', { origin: ORIGIN, fetchImpl: fake({ '/healthz': () => [401, {}] }) });
        const err = (await missing.health().catch((e: DaemonError) => e)) as DaemonError;
        expect(err.kind).toBe('auth');
        expect(err.message).toContain('needs its access token');
    });

    test('updateProject sends the options and hands back the file; a refusal says why', async () => {
        const calls: [string, RequestInit][] = [];
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: (async (input: RequestInfo | URL, init: RequestInit = {}) => {
            calls.push([new URL(String(input)).pathname, init]);
            const h = init.headers as Record<string, string>;
            if (h['X-Slice-Options'] === 'bad') return new Response('{"error":"spools must be a list"}', { status: 400 });
            return new Response(new Uint8Array([0x50, 0x4b]), { status: 200 });
        }) as FetchLike });
        const blob = await d.updateProject('jolteon.3mf', new Uint8Array([1]), '{"bed_type":"Supertack Plate"}');
        expect(new Uint8Array(await blob.arrayBuffer())).toEqual(new Uint8Array([0x50, 0x4b]));
        expect(calls[0][0]).toBe('/project/update');
        expect((calls[0][1].headers as Record<string, string>)['X-Filename']).toBe('jolteon.3mf');
        const e = await d.updateProject('x.3mf', new Uint8Array([1]), 'bad').catch((x) => x);
        expect(e).toBeInstanceOf(DaemonError);
        expect(e.message).toContain('spools must be a list');
    });

    test('unreachable vs origin not allowed', async () => {
        const down = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({ '/healthz': () => 'refuse' }) });
        const e1 = await down.health().catch((e) => e);
        expect(e1).toBeInstanceOf(DaemonError);
        expect(e1.kind).toBe('unreachable');
        expect(e1.message).toContain('--cors-origin https://beambam.boo');
        // CORS refusal: the normal request fails, the opaque no-cors probe answers.
        const cors = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/healthz': (init) => (init.mode === 'no-cors' ? [200, {}] : 'refuse') }) });
        const e2 = await cors.health().catch((e) => e);
        expect(e2.kind).toBe('origin');
    });

    test('403, 409 and 400 in plain words', async () => {
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/state': () => [403, { error: 'cross-origin request refused' }],
            '/slice': () => [409, { error: 'a slice is already running' }],
            '/print/upload': () => [400, { error: 'X-Filename must end in .gcode.3mf' }]
        }) });
        expect((await d.state().catch((e) => e)).kind).toBe('origin');
        const busy = await d.slice('a.3mf', new Uint8Array([80, 75]), null).catch((e) => e);
        expect(busy.kind).toBe('busy');
        expect(busy.message).toContain('a slice is already running');
        expect((await d.upload('x.3mf', new Uint8Array(1)).catch((e) => e)).message).toContain('must end in .gcode.3mf');
    });

    test('slice sends the options header, polls and returns the result', async () => {
        const calls: [string, RequestInit][] = [];
        let polls = 0;
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/slice': () => [202, { ok: true, poll: '/slice/jobs/s1' }],
            '/slice/jobs/s1': () => (++polls < 2 ? [200, { state: 'running', done: false, message: 'slicing' }]
                : [200, { state: 'done', done: true, file: '/x/a.gcode.3mf', seconds: 600, grams: 12.5, grams_per_filament: [10, 2.5], warnings: ['w'],
                    grams_by_filament: { 1: 10, 3: 2.5, 4: 'x' }, plate_seconds: [400, 200], plate_grams: [10, 2.5],
                    plate_grams_by_filament: [{ 1: 10 }, { 3: 2.5 }] }])
        }, calls) });
        const r = await d.slice('hoop.stl', new Uint8Array(84), '{"layer_height":0.16}');
        expect(r).toEqual({ file: '/x/a.gcode.3mf', seconds: 600, grams: 12.5, gramsPerFilament: [10, 2.5], warnings: ['w'],
            gramsByFilament: { 1: 10, 3: 2.5 }, plateSeconds: [400, 200], plateGrams: [10, 2.5],
            plateGramsByFilament: [{ 1: 10 }, { 3: 2.5 }] });
        const h = calls[0][1].headers as Record<string, string>;
        expect(h['X-Filename']).toBe('hoop.stl');
        expect(h['X-Slice-Options']).toBe('{"layer_height":0.16}');
        expect(h['Content-Type']).toBe('application/octet-stream');
    }, 10000);

    test('a failed slice says why', async () => {
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/slice': () => [202, { ok: true, poll: '/slice/jobs/s2' }],
            '/slice/jobs/s2': () => [200, { state: 'failed', done: true, message: 'slicing refused: Too small line width' }]
        }) });
        const e = await d.slice('a.3mf', new Uint8Array(2), null).catch((x) => x);
        expect(e.message).toBe('Slicing failed: slicing refused: Too small line width');
    }, 10000);

    test('a plan with errors (409) is returned, not thrown; JSON posts', async () => {
        const calls: [string, RequestInit][] = [];
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/print/plan': () => [409, { filaments: [], errors: ['tray A1 is empty'], warnings: [], notes: [], ok: false }],
            '/control/pause': () => [504, { error: 'no answer' }]
        }, calls) });
        const p = await d.plan('/x/a.gcode.3mf', [0]);
        expect(p.errors).toEqual(['tray A1 is empty']);
        expect(JSON.parse(String(calls[0][1].body))).toEqual({ file: '/x/a.gcode.3mf', slot: [0] });
        await d.plan('/x/a.gcode.3mf', null, 3);                       // a multi-plate project: the plate it prints
        expect(JSON.parse(String(calls[1][1].body))).toEqual({ file: '/x/a.gcode.3mf', plate: 3 });
        expect((calls[0][1].headers as Record<string, string>)['Content-Type']).toBe('application/json');
        expect((await d.control('pause')).ok).toBe(false);
    });

    test('LAN addresses ask for Local Network Access; loopback does not', async () => {
        const calls: [string, RequestInit][] = [];
        const routes = { '/healthz': (): [number, unknown] => [200, { healthy: true, version: '1.7.0', capabilities: [] }] };
        await new Daemon('http://192.168.1.20:8765', { origin: ORIGIN, fetchImpl: fake(routes, calls) }).health();
        await new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake(routes, calls) }).health();
        expect((calls[0][1] as { targetAddressSpace?: string }).targetAddressSpace).toBe('local');
        expect((calls[1][1] as { targetAddressSpace?: string }).targetAddressSpace).toBeUndefined();
    });
});

describe('jobs and starting', () => {
    test('a few failed polls are tolerated; a print 409 says why, with the new plan', async () => {
        let polls = 0;
        const sent: unknown[] = [];
        const d = new Daemon('http://127.0.0.1:8765', { origin: ORIGIN, fetchImpl: fake({
            '/slice': () => [202, { ok: true, poll: '/slice/jobs/s1' }],
            '/slice/jobs/s1': () => (++polls < 3 ? 'refuse'
                : [200, { state: 'done', done: true, file: '/x/a.gcode.3mf', seconds: 1, grams: 1, grams_by_filament: { '2': 1 },
                    plate_seconds: [1], plate_grams: [1], warnings: [] }]),
            '/healthz': () => [200, {}],
            '/print': (init) => (sent.push(JSON.parse(String(init.body))),
                [409, { ok: false, plan_changed: true, plan: { filaments: [], digest: 'b' }, error: 'changed' }])
        }) });
        const r = await d.slice('a.3mf', new Uint8Array(4), null);
        expect(r.gramsByFilament).toEqual({ '2': 1 });
        const e = await d.print('/x/a.gcode.3mf', [0], true, undefined, 'a', 2).catch((x) => x);
        expect(sent[0]).toEqual({ file: '/x/a.gcode.3mf', slot: [0], confirm: true, accept_warnings: true, plate: 2, plan_digest: 'a' });
        expect(e).toBeInstanceOf(DaemonError);
        expect(e.message).toContain('changed since this plan');
        expect(e.body.plan.digest).toBe('b');
    }, 20000);
});
