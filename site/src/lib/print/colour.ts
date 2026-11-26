/**
 * Colour helpers for filament swatches and spool matching.
 *
 * ΔE here is CIE76 (Euclidean distance in CIELAB, D65), the same measure beambam's
 * filament check and pokefit's slot picker report, so the badge on screen agrees with
 * the daemon's colour warnings. Ported from pokefit web/src/colour.ts (MIT, same author).
 */

/** "f72323ff" / "#F72323" / "F72323" -> "#F72323"; "" for anything that is not a colour. */
export function normHex(raw: string | null | undefined): string {
    const s = (raw ?? '').trim().replace(/^#/, '');
    return /^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(s) ? `#${s.slice(0, 6).toUpperCase()}` : '';
}

/** sRGB channels 0..1 of a #RRGGBB colour (black for anything unreadable). */
export function rgb01(hex: string): [number, number, number] {
    const h = normHex(hex) || '#000000';
    return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255) as [number, number, number];
}

/** Relative luminance (0..1) of a #RRGGBB colour, from gamma-encoded channels (good enough to pick text). */
export function luminance(hex: string): number {
    const [r, g, b] = rgb01(hex);
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** A text colour that reads on a swatch of `hex`. */
export function textOn(hex: string): string {
    return luminance(hex) > 0.55 ? '#111111' : '#ffffff';
}

/** sRGB hex -> CIELAB (D65). */
export function lab(hex: string): [number, number, number] {
    const lin = rgb01(hex).map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    const xyz = [
        (0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2]) / 0.95047,
        0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2],
        (0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2]) / 1.08883
    ];
    const eps = (6 / 29) ** 3;
    const f = xyz.map((t) => (t > eps ? Math.cbrt(t) : t / (3 * (6 / 29) ** 2) + 4 / 29));
    return [116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])];
}

/** CIE76 colour difference between two hex colours (0 = identical, ~2.3 = just noticeable). */
export function deltaE(a: string, b: string): number {
    const p = lab(a);
    const q = lab(b);
    return Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]);
}

/** How close a spool's colour is to a filament's: thresholds follow pokefit/remap.py. */
export type MatchLevel = 'good' | 'close' | 'far';

/** ΔE <= 10 reads as the same colour, <= 25 as close, anything above as a different colour. */
export function matchLevel(de: number): MatchLevel {
    return de <= 10 ? 'good' : de <= 25 ? 'close' : 'far';
}

/** "same colour" below ΔE 1, else "ΔE 12". */
export function deltaText(de: number): string {
    return de < 1 ? 'same colour' : `ΔE ${de.toFixed(0)}`;
}
