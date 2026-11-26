/**
 * The beambam daemon, called straight from this page (beambam.boo open in a browser on a
 * device that runs `beambam daemon --http ...` or can reach one on the LAN).
 *
 *   GET  /healthz            version + capabilities (503 = running, printer silent; still JSON)
 *   GET  /state              the printer's report
 *   POST /slice              raw project/mesh body -> 202 {poll} -> GET poll until done
 *   POST /print/upload       raw .gcode.3mf body -> {file}
 *   POST /print/plan         {file, slot?} -> filament -> spool plan (touches nothing)
 *   POST /print              {file, slot, confirm: true, accept_warnings} -> 202 {poll}
 *   POST /control/<verb>     pause | resume | stop with {}
 *
 * The daemon answers browser POSTs only from origins it was started with (`--cors-origin`).
 * Every failure becomes a `DaemonError` whose message says what to do in plain words.
 */

export const DEFAULT_URL = 'http://127.0.0.1:8765';
export const SITE_ORIGIN = 'https://beambam.boo';

const TIMEOUT_MS = 10_000;
const UPLOAD_TIMEOUT_MS = 180_000;
const SLICE_POLL_MS = 1500;
const PRINT_POLL_MS = 1500;
const SLICE_TIMEOUT_MS = 30 * 60_000;
/** Uploading to the printer, signing and its answer. */
const PRINT_TIMEOUT_MS = 240_000;

/** Why a call failed, so the page can show the right fix. */
export type FailKind = 'unreachable' | 'origin' | 'busy' | 'bad-request' | 'printer' | 'timeout' | 'http';

export class DaemonError extends Error {
    constructor(
        message: string,
        readonly kind: FailKind,
        readonly status = 0,
        readonly body: Record<string, unknown> = {}
    ) {
        super(message);
        this.name = 'DaemonError';
    }
}

type Json = Record<string, unknown>;

/** The part of `fetch` the client uses (injectable for tests). */
export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** "127.0.0.1:8765/" -> "http://127.0.0.1:8765"; throws on anything that is not an http(s) address. */
export function normaliseUrl(raw: string): string {
    const t = raw.trim().replace(/\/+$/, '');
    if (!t) throw new Error('enter the address beambam listens on, e.g. http://127.0.0.1:8765');
    const u = new URL(/^https?:\/\//i.test(t) ? t : `http://${t}`);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') throw new Error(`not a web address: ${raw}`);
    return `${u.protocol}//${u.host}`;
}

/** The command that starts a daemon this page may use. */
export function startCommand(origin: string = SITE_ORIGIN, url: string = DEFAULT_URL): string {
    let host = '127.0.0.1:8765';
    try {
        host = new URL(url).host;
    } catch {
        /* keep the default */
    }
    return `beambam daemon --http ${host} --cors-origin ${origin}`;
}

/** The host is this device (no Local Network Access "local" space needed). */
export function isLoopback(url: string): boolean {
    try {
        const h = new URL(url).hostname;
        return h === 'localhost' || h === '[::1]' || /^127\./.test(h);
    } catch {
        return false;
    }
}

/** "9 mm/s" style header values cannot carry non-ASCII: an ASCII file name for X-Filename. */
export function headerName(name: string, fallback = 'model.3mf'): string {
    const clean = name.replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+/, '').slice(-120);
    return clean && /\.[A-Za-z0-9]+$/.test(clean) ? clean : fallback;
}

/** `/healthz`. */
export interface Health {
    healthy: boolean;
    version: string;
    capabilities: string[];
    lastMessageAge: number | null;
    /** HTTP 503: the daemon runs but hears nothing from the printer. */
    printerSilent: boolean;
    /** The camera proxy behind /camera.mjpeg is up; null from a beambam without a camera relay. */
    camera: boolean | null;
}

/** A finished slice on the daemon's machine. */
export interface SliceResult {
    /** The .gcode.3mf's path on the daemon's machine (what /print/plan and /print take). */
    file: string;
    seconds: number;
    grams: number;
    gramsPerFilament: number[];
    /** Grams by filament number ("1", "2", …) over every plate; {} from an older beambam. */
    gramsByFilament: Record<string, number>;
    /** Per sliced plate: time and grams (a print starts plate 1); [] from an older beambam. */
    plateSeconds: number[];
    plateGrams: number[];
    warnings: string[];
}

/** Progress callback: seconds since the start and the daemon's own words. */
export type Tick = (seconds: number, what: string) => void;

/** Failed polls in a row before a slice / print job is given up on. */
const POLL_RETRIES = 4;

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** The error text in a daemon reply, if any. */
function replyError(body: Json): string {
    return typeof body.error === 'string' ? body.error : typeof body.message === 'string' ? body.message : '';
}

/**
 * A client for one daemon address. `fetchImpl` / `origin` are injectable for tests.
 */
export class Daemon {
    readonly url: string;
    private readonly fetchImpl: FetchLike;
    private readonly origin: string;

    constructor(url: string, opts: { fetchImpl?: FetchLike; origin?: string } = {}) {
        this.url = normaliseUrl(url);
        this.fetchImpl = opts.fetchImpl ?? ((input, init) => fetch(input, init));
        this.origin = opts.origin ?? (typeof location !== 'undefined' ? location.origin : SITE_ORIGIN);
    }

    /** The start command for this daemon and this page. */
    command(): string {
        return startCommand(this.origin, this.url);
    }

    private init(init: RequestInit): RequestInit {
        const out: RequestInit & { targetAddressSpace?: string } = { ...init, cache: 'no-store' };
        // Chrome's Local Network Access: an https page may call an http address on the LAN only
        // when it says so (loopback needs nothing: it is a secure origin already).
        if (!isLoopback(this.url) && this.origin.startsWith('https:')) out.targetAddressSpace = 'local';
        return out;
    }

    /**
     * Tell "not running" apart from "running but this origin is not allowed": a refused CORS
     * request and a refused connection look the same to a page, but an opaque no-cors request
     * still succeeds against a running server.
     */
    private async diagnoseNetworkFailure(timedOut: boolean): Promise<DaemonError> {
        if (timedOut) {
            return new DaemonError(
                `beambam at ${this.url} did not answer in time. If the browser asked to let this site reach ` +
                    `apps on this device, allow it and press Connect again; else check it is running: ${this.command()}`,
                'timeout'
            );
        }
        let running = false;
        try {
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), 4000);
            await this.fetchImpl(`${this.url}/healthz`, { ...this.init({}), mode: 'no-cors', signal: ctl.signal });
            clearTimeout(t);
            running = true;
        } catch {
            running = false;
        }
        if (running) {
            return new DaemonError(
                `beambam is running at ${this.url} but does not allow this page (${this.origin}). ` +
                    `Restart it with --cors-origin ${this.origin}: ${this.command()}`,
                'origin'
            );
        }
        return new DaemonError(
            `beambam is not reachable at ${this.url}. Start it on this device with: ${this.command()} — ` +
                'and if the browser asks to let this site reach apps on this device (Local Network Access), allow it.',
            'unreachable'
        );
    }

    /**
     * One request. Answers carrying a plan (409/422 with `filaments`) and a 503 /healthz are
     * returned rather than thrown: they still hold what the page shows.
     */
    async request(
        path: string,
        init: RequestInit = {},
        timeoutMs = TIMEOUT_MS,
        accept: (status: number, body: Json) => boolean = () => false
    ): Promise<{ status: number; body: Json }> {
        const ctl = new AbortController();
        let timedOut = false;
        const timer = setTimeout(() => {
            timedOut = true;
            ctl.abort();
        }, timeoutMs);
        let res: Response;
        try {
            res = await this.fetchImpl(this.url + path, { ...this.init(init), signal: ctl.signal });
        } catch {
            clearTimeout(timer);
            throw await this.diagnoseNetworkFailure(timedOut);
        }
        let body: Json = {};
        try {
            const parsed: unknown = await res.json();
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) body = parsed as Json;
        } catch {
            /* not JSON: reported by status below */
        } finally {
            clearTimeout(timer);
        }
        if (res.ok || accept(res.status, body)) return { status: res.status, body };
        const said = replyError(body);
        if (res.status === 403) {
            throw new DaemonError(
                `beambam refused this page (${this.origin}): ${said || 'origin not allowed'}. ` +
                    `Restart it with --cors-origin ${this.origin}.`,
                'origin',
                403,
                body
            );
        }
        if (res.status === 409) {
            throw new DaemonError(`beambam is busy: ${said || 'another job is running'}. Try again when it finishes.`,
                'busy', 409, body);
        }
        if (res.status === 400) throw new DaemonError(`beambam refused the request: ${said || 'bad request'}`, 'bad-request', 400, body);
        if (res.status === 503 || res.status === 504) {
            throw new DaemonError(`The printer is not answering: ${said || 'no report from it'}. Is it on and on this network?`,
                'printer', res.status, body);
        }
        throw new DaemonError(`beambam: ${said || `HTTP ${res.status}`}`, 'http', res.status, body);
    }

    private postJson(path: string, body: unknown, accept?: (s: number, b: Json) => boolean, timeoutMs?: number) {
        return this.request(
            path,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
            timeoutMs,
            accept
        );
    }

    /** `/healthz` (a 503 is a running daemon whose printer is silent). */
    async health(): Promise<Health> {
        const { status, body } = await this.request('/healthz', {}, TIMEOUT_MS, (s) => s === 503);
        return {
            healthy: body.healthy === true,
            version: typeof body.version === 'string' ? body.version : '',
            capabilities: Array.isArray(body.capabilities) ? body.capabilities.map(String) : [],
            lastMessageAge: typeof body.last_message_age_s === 'number' ? body.last_message_age_s : null,
            printerSilent: status === 503,
            camera: typeof body.camera === 'boolean' ? body.camera : null
        };
    }

    /**
     * The chamber camera's live MJPEG, for an <img> (multipart/x-mixed-replace renders natively).
     * `n` makes each (re)connect a new URL, so a dropped stream can be restarted.
     */
    cameraUrl(n = 0): string {
        return `${this.url}/camera.mjpeg${n ? `?n=${n}` : ''}`;
    }

    /**
     * Why the live view does not show, in beambam's words (its 503 text), or null when a frame
     * comes through now (the stream dropped and a reconnect should work).
     */
    async cameraProblem(): Promise<string | null> {
        try {
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), 25_000);
            const r = await this.fetchImpl(`${this.url}/snapshot.jpg`, { ...this.init({}), signal: ctl.signal });
            clearTimeout(t);
            if (r.ok) return null;
            return (await r.text()).trim() || `beambam answered HTTP ${r.status}`;
        } catch (e) {
            return `beambam did not answer (${e instanceof Error ? e.message : String(e)})`;
        }
    }

    /** The printer's report (the daemon's cached copy). */
    async state(): Promise<Json> {
        const { body } = await this.request('/state');
        if (!body.print || typeof body.print !== 'object') {
            throw new DaemonError('beambam has no report from the printer yet: is the printer on and on this network?', 'printer');
        }
        return body;
    }

    /**
     * Wait for a job until `done`; `finished(job)` returns the result or throws. A few failed polls in a row (a
     * network blip, the phone waking up) are tolerated: the job keeps running on the daemon either way.
     * `lost` words the failure when they do not stop.
     */
    private async pollJob<T>(poll: string, t0: number, everyMs: number, limitMs: number, onTick: Tick | undefined,
        finished: (job: Json) => T, tooLong: string, lost: (e: unknown) => DaemonError): Promise<T> {
        let failures = 0;
        for (;;) {
            await sleep(everyMs);
            let job: Json;
            try {
                job = (await this.request(poll)).body;
                failures = 0;
            } catch (e) {
                if (++failures < POLL_RETRIES && Date.now() - t0 <= limitMs) continue;
                throw lost(e);
            }
            onTick?.((Date.now() - t0) / 1000, String(job.message ?? job.state ?? ''));
            if (job.done === true) return finished(job);
            if (Date.now() - t0 > limitMs) throw new DaemonError(tooLong, 'timeout');
        }
    }

    /**
     * Slice a project or mesh on the daemon and wait for the print file. `options` is the
     * compact `X-Slice-Options` JSON (only sent when given).
     */
    async slice(name: string, bytes: Uint8Array | Blob, options: string | null, onTick?: Tick): Promise<SliceResult> {
        const t0 = Date.now();
        const headers: Record<string, string> = {
            'Content-Type': 'application/octet-stream',
            'X-Filename': headerName(name)
        };
        if (options) headers['X-Slice-Options'] = options;
        const { body } = await this.request('/slice', { method: 'POST', headers, body: bytes as BodyInit }, UPLOAD_TIMEOUT_MS);
        const poll = typeof body.poll === 'string' ? body.poll : '';
        if (!poll) throw new DaemonError('beambam did not start the slice', 'http');
        return this.pollJob(poll, t0, SLICE_POLL_MS, SLICE_TIMEOUT_MS, onTick, (job) => {
            if (job.state !== 'done' || typeof job.file !== 'string') {
                throw new DaemonError(`Slicing failed: ${String(job.message ?? job.error ?? 'unknown error')}`, 'http', 0, job);
            }
            return {
                file: job.file,
                seconds: Number(job.seconds ?? 0) || 0,
                grams: Number(job.grams ?? 0) || 0,
                gramsPerFilament: Array.isArray(job.grams_per_filament) ? job.grams_per_filament.map(Number) : [],
                gramsByFilament: job.grams_by_filament && typeof job.grams_by_filament === 'object'
                    ? Object.fromEntries(Object.entries(job.grams_by_filament as Record<string, unknown>).map(([k, v]) => [k, Number(v) || 0]))
                    : {},
                plateSeconds: Array.isArray(job.plate_seconds) ? job.plate_seconds.map(Number) : [],
                plateGrams: Array.isArray(job.plate_grams) ? job.plate_grams.map(Number) : [],
                warnings: Array.isArray(job.warnings) ? job.warnings.map(String) : []
            };
        }, 'Slicing is taking too long: check beambam', (e) => new DaemonError(
            `Lost track of the slice (${e instanceof Error ? e.message : String(e)}); it may still finish on beambam. Slice again when it answers.`,
            'http'));
    }

    /**
     * The project with `options` applied (the spools' filaments, the plate, print settings), as the
     * .3mf to keep: beambam's POST /project/update ("project-sync").
     */
    async updateProject(name: string, bytes: Uint8Array | Blob, options: string | null): Promise<Blob> {
        const headers: Record<string, string> = { 'Content-Type': 'application/octet-stream', 'X-Filename': headerName(name) };
        if (options) headers['X-Slice-Options'] = options;
        let res: Response;
        try {
            const ctl = new AbortController();
            const t = setTimeout(() => ctl.abort(), UPLOAD_TIMEOUT_MS);
            try {
                res = await this.fetchImpl(`${this.url}/project/update`,
                    { ...this.init({ method: 'POST', headers, body: bytes as BodyInit }), signal: ctl.signal });
            } finally {
                clearTimeout(t);
            }
        } catch {
            throw await this.diagnoseNetworkFailure(false);
        }
        if (!res.ok) {
            let said = '';
            try {
                said = replyError((await res.json()) as Json);
            } catch {
                /* not JSON */
            }
            throw new DaemonError(`beambam could not update the project: ${said || `HTTP ${res.status}`}`,
                res.status === 400 ? 'bad-request' : 'http', res.status);
        }
        return res.blob();
    }

    /** Hand an already-sliced .gcode.3mf to the daemon; resolves with its path there. */
    async upload(name: string, bytes: Uint8Array | Blob): Promise<string> {
        const { body } = await this.request('/print/upload', {
            method: 'POST',
            headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': headerName(name, 'print.gcode.3mf') },
            body: bytes as BodyInit
        }, UPLOAD_TIMEOUT_MS);
        if (typeof body.file !== 'string') throw new DaemonError('beambam did not keep the file', 'http');
        return body.file;
    }

    /** beambam's filament -> tray plan for `file` (null slots: beambam's own choice). Touches nothing. */
    async plan(file: string, slots: number[] | null): Promise<Json> {
        const { body } = await this.postJson('/print/plan', slots ? { file, slot: slots } : { file },
            (s, b) => (s === 409 || s === 422) && Array.isArray(b.filaments));
        return body;
    }

    /**
     * Start the print (the user confirmed). Resolves with the printer's last word once it accepted. `digest`
     * (the plan's): beambam starts only that plan, and answers 409 with the new one when spools, the plate or
     * the file changed since (a DaemonError whose `body.plan` is the new plan).
     */
    async print(file: string, slots: number[], acceptWarnings: boolean, onTick?: Tick, digest?: string): Promise<string> {
        const t0 = Date.now();
        const req: Json = { file, slot: slots, confirm: true, accept_warnings: acceptWarnings };
        if (digest) req.plan_digest = digest;
        // A 409 here is beambam refusing this print (mapping, warnings, a changed plan), not "busy": say why.
        const { status, body } = await this.postJson('/print', req, (s) => s === 409);
        if (status === 409 || body.ok === false) {
            const why = body.plan_changed === true ? 'The spools, the plate or the file changed since this plan: check the new plan, then Start again.'
                : body.needs_confirmation === true ? `beambam wants the warnings confirmed again: ${replyError(body)}`
                : `beambam refused the print: ${replyError(body) || 'unknown reason'}`;
            throw new DaemonError(why, 'bad-request', 409, body);
        }
        const poll = typeof body.poll === 'string' ? body.poll : '';
        if (!poll) throw new DaemonError('beambam did not start a print job', 'http');
        return this.pollJob(poll, t0, PRINT_POLL_MS, PRINT_TIMEOUT_MS, onTick, (job) => {
            if (job.state === 'accepted' || job.state === 'sent') return String(job.message ?? job.state);
            throw new DaemonError(`The print did not start (${String(job.state)}): ${String(job.message ?? '')}`, 'http', 0, job);
        }, 'No answer from the printer yet: check it before trying again', () => new DaemonError(
            'Lost contact with beambam after it took the print: it may have started. Check the printer before trying again.',
            'http'));
    }

    /**
     * Pause, resume or stop the current print. Resolves with whether the printer confirmed it
     * and a plain-words line (409 = the printer refused, 504 = it did not answer).
     */
    async control(verb: 'pause' | 'resume' | 'stop'): Promise<{ ok: boolean; text: string }> {
        const { status, body } = await this.postJson(`/control/${verb}`, {}, (s) => s === 409 || s === 504, 20_000);
        if (status === 409) return { ok: false, text: `The printer refused to ${verb}: ${replyError(body) || 'not possible now'}` };
        if (status === 504) return { ok: false, text: `Sent ${verb}, but the printer did not confirm it. Check the printer.` };
        const verified = body.verified === true;
        return { ok: true, text: verified ? `The printer confirmed: ${verb}.` : `Sent ${verb} to the printer.` };
    }
}

/** Parse a "1.7.0+source" version into [1, 7, 0]. */
export function parseVersion(v: string): number[] {
    return (v.match(/\d+(\.\d+)*/)?.[0] ?? '0').split('.').map(Number);
}

/** `a` >= `b` for dotted versions. */
export function versionAtLeast(a: string, b: string): boolean {
    const x = parseVersion(a);
    const y = parseVersion(b);
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
        const d = (x[i] ?? 0) - (y[i] ?? 0);
        if (d !== 0) return d > 0;
    }
    return true;
}
