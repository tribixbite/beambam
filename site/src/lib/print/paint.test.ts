import { describe, expect, test } from 'bun:test';
import { encodePaint, majorityState, paintDecoder, paintWeights, rootState } from './paint';

describe('paint_color decoding', () => {
    test('whole-triangle states (pokefit/paint.py examples)', () => {
        expect(rootState('4')).toBe(1);
        expect(rootState('8')).toBe(2);
        expect(rootState('0C')).toBe(3);
        expect(rootState('1C')).toBe(4);
        expect(rootState('')).toBe(0);
    });
    test('state 18+ uses 0xF continuation nibbles', () => {
        // leaf 0xC, then F (+15), then 0 -> 3 + 15 + 0 = 18; read from the end: "0FC"
        expect(rootState('0FC')).toBe(18);
    });
    test('a split triangle has no root state; majority picks the largest share', () => {
        // Nibbles read last-first: 0x3 = split 3 sides (4 children), then leaves 8, 8, 8, 4.
        const code = '4888' + '3';
        expect(rootState(code)).toBeNull();
        const w = paintWeights(code);
        expect(w.get(2)).toBeCloseTo(0.75);
        expect(w.get(1)).toBeCloseTo(0.25);
        expect(majorityState(code)).toBe(2);
    });
    test('encodePaint is the inverse of rootState for whole-triangle leaves', () => {
        expect(encodePaint(0)).toBe('');     // unpainted: no attribute
        expect(encodePaint(1)).toBe('4');
        expect(encodePaint(2)).toBe('8');
        expect(encodePaint(3)).toBe('0C');
        expect(encodePaint(4)).toBe('1C');
        expect(encodePaint(18)).toBe('0FC');
    });
    test('encode/decode round-trips every filament state 1..40', () => {
        for (let s = 1; s <= 40; s++) {
            const code = encodePaint(s);
            expect(rootState(code)).toBe(s);
            expect(majorityState(code)).toBe(s);
        }
    });
    test('memoised decoder agrees', () => {
        const d = paintDecoder();
        expect(d('8')).toBe(2);
        expect(d('8')).toBe(2);
        expect(d('1C')).toBe(4);
    });
});
