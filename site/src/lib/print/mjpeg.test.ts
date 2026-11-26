import { describe, expect, test } from 'bun:test';
import { boundaryOf, MjpegParser, newestFrame, readMjpeg } from './mjpeg';

const enc = new TextEncoder();
/** A JPEG-ish frame (SOI … EOI) whose bytes include CRLFs and a fake boundary, as real JPEG data can. */
const jpeg = (n: number): Uint8Array => new Uint8Array([0xff, 0xd8, n, 13, 10, 13, 10, ...enc.encode('--frame'), n, 0xff, 0xd9]);

/** One part as beambam's relay writes it (`beambam camera`: with Content-Length). */
function part(f: Uint8Array, withLength = true): Uint8Array {
    const head = enc.encode(`--frame\r\nContent-Type: image/jpeg\r\n${withLength ? `Content-Length: ${f.length}\r\n` : ''}\r\n`);
    const out = new Uint8Array(head.length + f.length + 2);
    out.set(head);
    out.set(f, head.length);
    out.set([13, 10], head.length + f.length);
    return out;
}
const concat = (...xs: Uint8Array[]): Uint8Array => {
    const out = new Uint8Array(xs.reduce((n, x) => n + x.length, 0));
    let at = 0;
    for (const x of xs) {
        out.set(x, at);
        at += x.length;
    }
    return out;
};

describe('MJPEG', () => {
    test('boundary from the Content-Type', () => {
        expect(boundaryOf('multipart/x-mixed-replace; boundary=frame')).toBe('frame');
        expect(boundaryOf('multipart/x-mixed-replace;boundary="a b"')).toBe('a b');
        expect(boundaryOf('image/jpeg')).toBeNull();
    });

    test('frames come out whole whatever the chunking, by Content-Length', () => {
        const stream = concat(part(jpeg(1)), part(jpeg(2)), part(jpeg(3)));
        for (const size of [1, 3, 7, 64, stream.length]) {
            const p = new MjpegParser('frame');
            const got: Uint8Array[] = [];
            for (let i = 0; i < stream.length; i += size) got.push(...p.push(stream.subarray(i, i + size)));
            expect(got).toEqual([jpeg(1), jpeg(2), jpeg(3)]);
        }
    });

    test('without Content-Length a part ends at the next boundary', () => {
        const p = new MjpegParser('frame');
        // A body free of the boundary text (only the real separator ends a length-less part).
        const plain = (n: number) => new Uint8Array([0xff, 0xd8, n, n, 0xff, 0xd9]);
        expect(p.push(concat(part(plain(1), false), part(plain(2), false)))).toEqual([plain(1)]);
        expect(p.push(enc.encode('--frame\r\n'))).toEqual([plain(2)]);
    });

    test('a stream that is not MJPEG is refused instead of buffered forever', () => {
        const p = new MjpegParser('frame');
        expect(() => p.push(new Uint8Array(20_000).fill(65))).toThrow('not MJPEG');
        const q = new MjpegParser('frame');
        expect(() => q.push(enc.encode('--frame\r\nContent-Length: 99999999\r\n\r\n'))).toThrow('too large');
    });

    test('readMjpeg hands each frame over as a JPEG blob and ends with the stream', async () => {
        const body = concat(part(jpeg(1)), part(jpeg(2)));
        const res = new Response(new ReadableStream({
            start(c) {
                c.enqueue(body.subarray(0, 10));
                c.enqueue(body.subarray(10));
                c.close();
            }
        }), { headers: { 'Content-Type': 'multipart/x-mixed-replace; boundary=frame' } });
        const got: Uint8Array[] = [];
        await readMjpeg(res, (b) => {
            expect(b.type).toBe('image/jpeg');
            got.push(new Uint8Array(0));
        });
        expect(got.length).toBe(2);
    });

    test('newestFrame decodes before it shows, drops the backlog, and clears on abort', () => {
        const g = globalThis as unknown as { Image: unknown };
        const saved = { Image: g.Image, create: URL.createObjectURL, revoke: URL.revokeObjectURL };
        const loads: (() => void)[] = [];
        class FakeImage {
            onload: (() => void) | null = null;
            onerror: (() => void) | null = null;
            set src(_: string) {
                loads.push(() => this.onload?.());
            }
        }
        let n = 0;
        const revoked: string[] = [];
        g.Image = FakeImage;
        URL.createObjectURL = () => `blob:${++n}`;
        URL.revokeObjectURL = (u: string) => void revoked.push(u);
        try {
            const shown: string[] = [];
            const ctl = new AbortController();
            const push = newestFrame((u) => shown.push(u), ctl.signal);
            const b = new Blob([new Uint8Array(1)]);
            push(b); // decoding frame 1
            push(b); // waiting…
            push(b); // …replaced by the newest: one decode follows, not two
            expect(shown).toEqual([]);
            loads.shift()!();
            loads.shift()!();
            expect(loads.length).toBe(0);
            expect(shown).toEqual(['blob:1', 'blob:2']);
            expect(revoked).toEqual(['blob:1']);
            ctl.abort();
            expect(shown.at(-1)).toBe('');
            expect(revoked).toEqual(['blob:1', 'blob:2']);
        } finally {
            g.Image = saved.Image;
            URL.createObjectURL = saved.create;
            URL.revokeObjectURL = saved.revoke;
        }
    });
});
