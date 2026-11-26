import { describe, expect, test } from 'bun:test';
import { buildSliceOptions, closestTray, isTwoNozzlePlan, nozzleMismatches, nozzlesWith, optionsHeader, PROJECT_SETTINGS, slotsFromPlan,
    spoolChoices, withSlot, type PrintPlan } from './plan';
import { readTrays } from './printer';

const plan: PrintPlan = {
    filaments: [
        { filament: 1, type: 'PLA', colour: '#EEBF31', nozzle: 'left', tray: { tray_id: 3, label: 'A4 (slot 3)', type: 'PLA', colour: '#F98C36' }, errors: [], warnings: ['colour'] },
        { filament: 3, type: 'PLA', colour: '#FFFFFF', nozzle: 'right', tray: { tray_id: 7, label: 'B4 (slot 7)', type: 'PLA', colour: '#FFFFFF' }, errors: [], warnings: [] }
    ],
    errors: [], warnings: [], notes: []
};

describe('slice options', () => {
    test('project defaults send nothing', () => {
        expect(buildSliceOptions(PROJECT_SETTINGS)).toBeNull();
    });
    test('only changed settings, nozzles and the mesh spool', () => {
        const o = buildSliceOptions({ ...PROJECT_SETTINGS, layerHeight: 0.16, walls: 3, infill: 25, supports: 'plate', brim: 'no_brim' },
            { profileId: 'GFG02', type: 'PETG', colour: '#FF0000' }, [2]);
        expect(optionsHeader(o!)).toBe('{"layer_height":0.16,"wall_loops":3,"infill":25,"supports":"plate","brim":"no_brim","nozzles":[2],"filament_ids":["GFG02"],"filament_types":["PETG"]}');
    });
    test('an empty preset id is left out', () => {
        expect(buildSliceOptions(PROJECT_SETTINGS, { profileId: '', type: 'PLA', colour: '' })).toEqual({ filament_types: ['PLA'] });
    });
});

describe('plan helpers', () => {
    test('nozzlesWith fills unlisted filaments with left and moves one', () => {
        expect(nozzlesWith(plan, 1, 2)).toEqual([2, 1, 2]);
        expect(nozzlesWith(plan, 3, 1)).toEqual([1, 1, 1]);
        expect(isTwoNozzlePlan(plan)).toBe(true);
        expect(isTwoNozzlePlan({ ...plan, filaments: plan.filaments.map((f) => ({ ...f, nozzle: '-' })) })).toBe(false);
    });
    test('nozzle mismatches between the request and the slice', () => {
        expect(nozzleMismatches(plan, [1, 1, 2])).toEqual([]);
        expect(nozzleMismatches(plan, [2, 1, 1])).toEqual(['filament 1 (asked right, sliced left)', 'filament 3 (asked left, sliced right)']);
        expect(nozzleMismatches(plan, null)).toEqual([]);
    });
    test('slots from a plan and moving one row', () => {
        expect(slotsFromPlan(plan)).toEqual([3, 7]);
        expect(slotsFromPlan({ ...plan, filaments: [{ ...plan.filaments[0], tray: null }] })).toBeNull();
        expect(withSlot(plan, null, 1, 12)).toEqual([3, 12]);
    });
    test('spool choices: closest first, a vanished current tray kept last', () => {
        const trays = readTrays({ print: { ams: { ams: [{ id: '0', tray: [
            { id: '0', tray_type: 'PLA', tray_color: '000000FF' }, { id: '1', tray_type: 'PLA', tray_color: 'EEC030FF' },
            { id: '2', tray_type: '' }] }] } } });
        const c = spoolChoices(trays, '#EEBF31', 9);
        expect(c.map((x) => x.trayId)).toEqual([1, 0, 9]);
        expect(c[0].de).toBeLessThan(2);
        expect(c[2]).toMatchObject({ unavailable: true, detail: 'not loaded now' });
        expect(closestTray(trays, '#101010', 'PLA')?.trayId).toBe(0);
    });
    test('the filament\'s own material ranks before a closer colour in another', () => {
        const trays = readTrays({ print: { ams: { ams: [{ id: '0', tray: [
            { id: '0', tray_type: 'PETG', tray_color: 'EEBF31FF' }, { id: '1', tray_type: 'PLA', tray_color: 'F4EE2AFF' },
            { id: '2', tray_type: 'PLA', tray_color: '000000FF' }] }] } } });
        expect(spoolChoices(trays, '#EEBF31', null, 'pla').map((x) => x.trayId)).toEqual([1, 2, 0]);
        expect(spoolChoices(trays, '#EEBF31', null).map((x) => x.trayId)).toEqual([0, 1, 2]);
    });
});
