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
        // Filament-indexed (filaments 1 and 3 used): beambam reads a list with -1 by filament number.
        expect(slotsFromPlan(plan)).toEqual([3, -1, 7]);
        expect(slotsFromPlan({ ...plan, filaments: [{ ...plan.filaments[0], tray: null }] })).toBeNull();
        expect(withSlot(plan, null, 1, 12)).toEqual([3, -1, 12]);         // row 1 is filament 3
        expect(withSlot(plan, [3, -1, 7], 0, 5)).toEqual([5, -1, 7]);
        expect(optionsHeader({ filament_types: ['PLA-Ü'] })).toBe('{"filament_types":["PLA-\\u00dc"]}');
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

describe('the plate setting', () => {
    test('is sent as bed_type only when chosen, and read in the maker\'s words', async () => {
        const { buildSliceOptions, PROJECT_SETTINGS, plateText } = await import('./plan');
        expect(buildSliceOptions(PROJECT_SETTINGS)).toBeNull();
        expect(buildSliceOptions({ ...PROJECT_SETTINGS, bed: 'Supertack Plate' })).toEqual({ bed_type: 'Supertack Plate' });
        expect(plateText('Supertack Plate')).toBe('SuperTack plate');
        expect(plateText('Textured PEI Plate')).toBe('Textured PEI plate');
    });
});

describe('project and printer', () => {
    test('rows compare what the file prints with against its spool; the spools option keeps unused filaments', async () => {
        const { projectVsSpools, spoolsFromPlan } = await import('./plan');
        const withId: PrintPlan = { ...plan, filaments: plan.filaments.map((f) => ({ ...f, tray: f.tray && { ...f.tray, profile_id: 'GFA00' } })) };
        const s = projectVsSpools(withId);
        expect(s.differs).toBe(true);
        expect(s.rows.map((r) => [r.filament, r.same])).toEqual([[1, false], [3, true]]);
        const spools = spoolsFromPlan([{ type: 'PLA', colour: '#EEBF31' }, { type: 'PETG', colour: '#123456' }, { type: 'PLA', colour: '#FFFFFF' }], withId);
        expect(spools).toEqual([
            { id: 'GFA00', type: 'PLA', colour: '#F98C36' },
            { id: null, type: null, colour: '#123456' },          // filament 2: not on this plate, stays
            { id: 'GFA00', type: 'PLA', colour: '#FFFFFF' }
        ]);
        expect(buildSliceOptions(PROJECT_SETTINGS, null, null, spools)?.spools).toEqual(spools);
        const matched: PrintPlan = { ...plan, filaments: [plan.filaments[1]] };
        expect(projectVsSpools(matched).differs).toBe(false);
    });
});
