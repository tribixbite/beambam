import { describe, expect, test } from 'bun:test';
import { gramsText, isBusy, secondsText, loadedTrays, minutesText, readStatus, readTrays, trayLabel, traySignature, unpackTemp } from './printer';

/** A trimmed copy of a live X2D report: two AMS 2 Pro units on a Filament Track Switch. */
const STATE = {
    print: {
        gcode_state: 'RUNNING', mc_percent: 29, mc_remaining_time: 35, layer_num: 19, total_layer_num: 125,
        subtask_name: 'Keychain', bed_temper: 45, bed_target_temper: 45, aux: '22801004',
        device: { extruder: { info: [{ id: 0, temp: 36 }, { id: 1, temp: 14418140 }] } },
        ams: {
            ams: [
                { id: '0', info: '10001E03', tray: [{ id: '0', tray_type: 'PETG', tray_color: 'F72323FF', tray_info_idx: 'GFG99' },
                    { id: '1', tray_type: '', tray_color: '00000000' }] },
                { id: '1', info: '11001E03', tray: [{ id: '3', tray_type: 'PLA', tray_color: 'FFFFFFFF', tray_sub_brands: 'PLA Pure', remain: 82 }] },
                { id: '128', info: '10000104', tray: [{ id: '0', tray_type: 'PETG', tray_color: '161616FF' }] }
            ]
        },
        vir_slot: [{ id: '254', tray_type: 'PLA', tray_color: '898989FF' }, { id: '255', tray_type: '' }]
    }
};

describe('readTrays', () => {
    const trays = readTrays(STATE);
    test('global ids and labels', () => {
        expect(trays.map((t) => [t.trayId, t.label])).toEqual([
            [0, 'A1'], [1, 'A2'], [7, 'B4'], [128, 'HT1'], [254, 'Ext L'], [255, 'Ext R']
        ]);
    });
    test('material, colour, empties, reach', () => {
        expect(trays[0]).toMatchObject({ type: 'PETG', colour: '#F72323', profileId: 'GFG99', reach: 'both', empty: false });
        expect(trays[1].empty).toBe(true);
        expect(trays[2]).toMatchObject({ name: 'PLA Pure', remain: 82, unitType: 'AMS 2 Pro' });
        expect(trays[3].unitType).toBe('AMS HT');
        expect(trays[3].reach).toBe('left');
        expect(trays[4].reach).toBe('left');
        expect(loadedTrays(trays).map((t) => t.trayId)).toEqual([0, 7, 128, 254]);
    });
    test('signature changes when a spool changes', () => {
        const a = traySignature(trays);
        const swapped = structuredClone(STATE);
        swapped.print.ams.ams[0].tray[0].tray_color = '00FF00FF';
        expect(traySignature(readTrays(swapped))).not.toBe(a);
        expect(traySignature(readTrays(structuredClone(STATE)))).toBe(a);
    });
    test('labels on a one-nozzle printer', () => {
        expect(trayLabel(255, false)).toBe('Ext');
        expect(trayLabel(9)).toBe('C2');
    });
});

describe('readStatus', () => {
    test('job, layers and packed nozzle temps, left first', () => {
        const s = readStatus(STATE);
        expect(s).toMatchObject({ state: 'RUNNING', percent: 29, remainingMin: 35, layer: 19, totalLayers: 125, job: 'Keychain', dual: true });
        expect(s.nozzles).toEqual([{ name: 'Left', current: 220, target: 220 }, { name: 'Right', current: 36, target: 0 }]);
        expect(isBusy(s.state)).toBe(true);
    });
    test('single nozzle printers use nozzle_temper', () => {
        const s = readStatus({ print: { gcode_state: 'IDLE', nozzle_temper: 25.5, nozzle_target_temper: 0 } });
        expect(s.nozzles).toEqual([{ name: 'Nozzle', current: 25.5, target: 0 }]);
        expect(isBusy(s.state)).toBe(false);
    });
    test('unpack and time text', () => {
        expect(unpackTemp(220 | (230 << 16))).toEqual({ current: 220, target: 230 });
        expect(minutesText(35)).toBe('35 min');
        expect(minutesText(59.7)).toBe('1 h 00 min');
        expect(minutesText(125)).toBe('2 h 05 min');
        expect(secondsText(15.6)).toBe('16 s');
        expect(secondsText(9864)).toBe('2 h 44 min');
        expect(gramsText(0.04)).toBe('< 0.1 g');
        expect(gramsText(42.46)).toBe('42.5 g');
    });
});

describe('the plate on the printer', () => {
    test('a known marker is named, another stays a marker, none is null', async () => {
        const { readPlate, readStatus } = await import('./printer');
        const on = (cur_id: string) => ({ print: { device: { plate: { base: 9, cur_id, mat: 1, tar_id: '' } } } });
        expect(readPlate(on('P0301'))).toEqual({ marker: 'P0301', name: 'Supertack Plate' });
        expect(readPlate(on('P0999'))).toEqual({ marker: 'P0999', name: null });
        expect(readPlate({ print: {} })).toBeNull();
        expect(readStatus(on('P0301')).plate?.name).toBe('Supertack Plate');
    });
});
