/**
 * The chamber camera's MJPEG (`GET /camera.mjpeg`, multipart/x-mixed-replace) read with `fetch` instead of an
 * `<img>`: an image request cannot carry the daemon's access token (`Authorization`), cannot ask Chrome's Local
 * Network Access for a LAN address (`targetAddressSpace`), and is only closed by clearing its src. A fetch does
 * all three, and an AbortController ends it.
 *
 * Each part is `--boundary`, headers, a blank line and one JPEG. beambam's relay (and `beambam camera`) send
 * `Content-Length`; without it a part ends at the next boundary.
 */

const CRLF2 = new Uint8Array([13, 10, 13, 10]);
/** A part's headers are a few lines; more than this without a blank line is not an MJPEG stream. */
const MAX_HEAD = 16 * 1024;
/** One frame (a 1080p JPEG is ~100 KB here); more is a broken stream, not a picture. */
const MAX_FRAME = 8 * 1024 * 1024;

function indexOf(hay: Uint8Array, needle: Uint8Array, from = 0): number {
    const first = needle[0];
    const last = hay.length - needle.length;
    outer: for (let i = hay.indexOf(first, from); i !== -1 && i <= last; i = hay.indexOf(first, i + 1)) {
        for (let k = 1; k < needle.length; k++) if (hay[i + k] !== needle[k]) continue outer;
        return i;
    }
    return -1;
}

/** The boundary named in a multipart Content-Type, or null. */
export function boundaryOf(contentType: string | null): string | null {
    const m = /boundary="?([^";]+)"?/i.exec(contentType ?? '');
    return m ? m[1].trim() : null;
}

/** Cuts a multipart MJPEG byte stream into JPEG frames, whatever the chunking. */
export class MjpegParser {
    private buf = new Uint8Array(0);
    private readonly next: Uint8Array | null;

    /** `boundary`: from the response's Content-Type (`boundaryOf`); used only for parts without a length. */
    constructor(boundary: string | null) {
        this.next = boundary ? new TextEncoder().encode(`\r\n--${boundary}`) : null;
    }

    /** Add bytes; returns the frames they complete (often none, sometimes several). Throws on a broken stream. */
    push(chunk: Uint8Array): Uint8Array[] {
        const joined = new Uint8Array(this.buf.length + chunk.length);
        joined.set(this.buf);
        joined.set(chunk, this.buf.length);
        this.buf = joined;
        const frames: Uint8Array[] = [];
        for (;;) {
            const headEnd = indexOf(this.buf, CRLF2);
            if (headEnd < 0) {
                if (this.buf.length > MAX_HEAD) throw new Error('the camera stream is not MJPEG');
                break;
            }
            const head = new TextDecoder('latin1').decode(this.buf.subarray(0, headEnd));
            const start = headEnd + 4;
            const len = /content-length:\s*(\d+)/i.exec(head);
            let end: number;
            let rest: number;
            if (len) {
                end = start + Number(len[1]);
                if (end - start > MAX_FRAME) throw new Error('the camera sent a frame too large to show');
                if (this.buf.length < end) break;
                rest = end;
            } else if (this.next) {
                end = indexOf(this.buf, this.next, start);
                if (end < 0) {
                    if (this.buf.length - start > MAX_FRAME) throw new Error('the camera sent a frame too large to show');
                    break;
                }
                rest = end + 2; // keep "--boundary" for the next part's head
            } else {
                throw new Error('the camera stream names no boundary and no frame length');
            }
            frames.push(this.buf.slice(start, end));
            this.buf = this.buf.slice(rest);
        }
        return frames;
    }
}

/**
 * Read `res` (an MJPEG response) to its end, handing each frame to `onFrame` as a JPEG blob. Resolves when the
 * stream ends; rejects on a read error or a broken stream (an abort rejects with the AbortError).
 */
export async function readMjpeg(res: Response, onFrame: (jpeg: Blob) => void): Promise<void> {
    if (!res.body) throw new Error('this browser cannot stream the camera');
    const parser = new MjpegParser(boundaryOf(res.headers.get('Content-Type')));
    const reader = res.body.getReader();
    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (done) return;
            for (const f of parser.push(value)) onFrame(new Blob([f as BlobPart], { type: 'image/jpeg' }));
        }
    } finally {
        reader.releaseLock();
    }
}

/**
 * Show the newest frame only: each JPEG is decoded before it replaces the one on screen (whole frames, no
 * flicker), and frames that arrive meanwhile are dropped but the latest, so a slow phone falls behind by at most
 * one frame instead of building a backlog. `show` gets an object URL (or '' once `signal` aborts); the previous
 * URL is revoked after it is replaced. Returns the `onFrame` for `readMjpeg`.
 */
export function newestFrame(show: (url: string) => void, signal: AbortSignal): (jpeg: Blob) => void {
    let pending: Blob | null = null;
    let decoding = false;
    let current = '';
    const replace = (url: string): void => {
        const old = current;
        current = url;
        show(url);
        if (old) URL.revokeObjectURL(old);
    };
    signal.addEventListener('abort', () => {
        pending = null;
        replace('');
    }, { once: true });
    const next = (): void => {
        if (!pending || decoding || signal.aborted) return;
        const url = URL.createObjectURL(pending);
        pending = null;
        decoding = true;
        const img = new Image();
        img.onload = img.onerror = () => {
            decoding = false;
            if (signal.aborted) URL.revokeObjectURL(url);
            else replace(url);
            next();
        };
        img.src = url;
    };
    return (jpeg) => {
        pending = jpeg;
        next();
    };
}
