import { describe, expect, test } from 'bun:test';
import { deltaE, deltaText, matchLevel, normHex, textOn } from './colour';

describe('colour', () => {
    test('normHex accepts printer and project spellings', () => {
        expect(normHex('f72323ff')).toBe('#F72323');
        expect(normHex('#eebf31')).toBe('#EEBF31');
        expect(normHex('')).toBe('');
        expect(normHex('nope')).toBe('');
    });
    test('deltaE: identical is 0, black/white is 100', () => {
        expect(deltaE('#123456', '#123456')).toBeCloseTo(0, 6);
        expect(deltaE('#000000', '#FFFFFF')).toBeCloseTo(100, 0);
    });
    test('match levels and text', () => {
        expect(matchLevel(4)).toBe('good');
        expect(matchLevel(18)).toBe('close');
        expect(matchLevel(40)).toBe('far');
        expect(deltaText(0.4)).toBe('same colour');
        expect(deltaText(12.3)).toBe('ΔE 12');
    });
    test('readable text on swatches', () => {
        expect(textOn('#FFFFFF')).toBe('#111111');
        expect(textOn('#000000')).toBe('#ffffff');
    });
});
