/**
 * Reading the printer's report (`GET /state` -> {"print": {...}}): the loaded spools and what
 * the printer is doing. Mirrors beambam/filament_mapping.py `read_topology` so tray ids and
 * labels match what `/print/plan` and the printer's touchscreen show.
 *
 * Tray ids: an AMS unit `ams_id` holds trays ams_id*4 + slot (labels A1..D4 for units 0..3);
 * an AMS HT (`ams_id` >= 128, or unit type 4) holds one tray, id = ams_id + slot; the external
 * spools are 254 (left / deputy nozzle) and 255 (right / main nozzle).
 */
import { normHex } from './colour';

/** Which nozzle(s) a tray can feed. */
export type Reach = 'left' | 'right' | 'both' | 'none' | 'unknown';

/** One spool position on the printer. */
export interface Tray {
    /** Global tray id, as /print/plan and /print take it. */
    trayId: number;
    /** Touchscreen label: "A1", "HT1", "Ext R". */
    label: string;
    amsId: number;
    slot: number;
    /** Material ("PLA"); "" when the tray is empty. */
    type: string;
    /** "#RRGGBB" ("" when unknown). */
    colour: string;
    /** Bambu filament preset id (tray_info_idx, e.g. "GFA00"). */
    profileId: string;
    /** Sub-brand ("PLA Basic"), "" when the spool has no RFID. */
    name: string;
    /** Percent left, or -1 when the AMS does not know. */
    remain: number;
    /** AMS model ("AMS 2 Pro"), or "External". */
    unitType: string;
    reach: Reach;
    empty: boolean;
}

/** A nozzle's temperature (°C). */
export interface NozzleTemp {
    name: string;
    current: number;
    target: number;
}

/** What the printer is doing, as the job view shows it. */
export interface PrinterStatus {
    /** gcode_state: IDLE, PREPARE, RUNNING, PAUSE, FINISH, FAILED, "" when unknown. */
    state: string;
    percent: number;
    /** Minutes left (mc_remaining_time). */
    remainingMin: number;
    layer: number;
    totalLayers: number;
    job: string;
    bed: number;
    bedTarget: number;
    nozzles: NozzleTemp[];
    /** Two nozzles (H2D / X2D). */
    dual: boolean;
}

type Obj = Record<string, unknown>;

const AMS_TYPES: Record<number, string> = { 1: 'AMS', 2: 'AMS Lite', 3: 'AMS 2 Pro', 4: 'AMS HT' };
const EXT_LEFT = 254;
const EXT_RIGHT = 255;

function obj(v: unknown): Obj {
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : {};
}

function arr(v: unknown): unknown[] {
    return Array.isArray(v) ? v : [];
}

function int(v: unknown, fallback: number): number {
    const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;
    return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

function num(v: unknown, fallback = 0): number {
    const n = typeof v === 'number' ? v : typeof v === 'string' ? Number(v) : NaN;
    return Number.isFinite(n) ? n : fallback;
}

function str(v: unknown): string {
    return typeof v === 'string' ? v.trim() : '';
}

/** A hex string field ("10001E03", "40029FD1B30F9CB3") as a BigInt (0n when absent). */
function hexBig(v: unknown): bigint {
    const s = str(v);
    return /^[0-9a-f]+$/i.test(s) ? BigInt(`0x${s}`) : 0n;
}

function bits(value: bigint, start: number, count: number): number {
    return Number((value >> BigInt(start)) & ((1n << BigInt(count)) - 1n));
}

/** The report's inner dict, from `{"print": {...}}` or the dict itself. */
export function report(state: unknown): Obj {
    const s = obj(state);
    return 'print' in s ? obj(s.print) : s;
}

/** "A1".."D4" for normal AMS trays, "HT1" for an AMS HT, "Ext L" / "Ext R" for the external spools. */
export function trayLabel(trayId: number, dual = true): string {
    if (trayId === EXT_RIGHT) return dual ? 'Ext R' : 'Ext';
    if (trayId === EXT_LEFT) return dual ? 'Ext L' : 'Ext';
    if (trayId >= 128) return `HT${trayId - 127}`;
    return `${String.fromCharCode(65 + Math.floor(trayId / 4))}${(trayId % 4) + 1}`;
}

/** beambam's tray label without its " (slot N)" suffix: "A1 (slot 0)" -> "A1". */
export function shortLabel(label: string): string {
    return label.replace(/\s*\(slot \d+\)$/, '');
}

/** Every tray the printer reports (AMS units, AMS HT, external spools), empty ones included. */
export function readTrays(state: unknown): Tray[] {
    const p = report(state);
    const extruders = arr(obj(obj(p.device).extruder).info);
    const dual = extruders.length >= 2;
    // aux bit 29: a Filament Track Switch is installed (any AMS tray reaches either nozzle).
    const switchInstalled = bits(hexBig(p.aux), 29, 1) === 1;
    const out: Tray[] = [];

    for (const u of arr(obj(p.ams).ams)) {
        const unit = obj(u);
        const amsId = int(unit.id, -1);
        if (amsId < 0) continue;
        const infoRaw = str(unit.info);
        const info = hexBig(infoRaw);
        const typeId = infoRaw ? bits(info, 0, 4) : 1;
        const ext = bits(info, 8, 4);
        let reach: Reach = 'unknown';
        if (!dual) reach = 'right';
        else if (!infoRaw) reach = 'unknown';
        else if (ext === 0xe) reach = switchInstalled && bits(info, 24, 4) <= 1 ? 'both' : 'none';
        else if (ext === 0) reach = 'right';
        else if (ext === 1) reach = 'left';
        const ht = typeId === 4 || amsId >= 128;
        for (const t of arr(unit.tray)) {
            const tray = obj(t);
            const slot = int(tray.id, -1);
            if (slot < 0) continue;
            const trayId = ht ? amsId + slot : amsId * 4 + slot;
            const type = str(tray.tray_type);
            out.push({
                trayId,
                label: trayLabel(trayId, dual),
                amsId,
                slot,
                type,
                colour: normHex(str(tray.tray_color)),
                profileId: str(tray.tray_info_idx),
                name: str(tray.tray_sub_brands),
                remain: int(tray.remain, -1),
                unitType: AMS_TYPES[typeId] ?? 'AMS',
                reach,
                empty: type === ''
            });
        }
    }

    for (const v of arr(p.vir_slot)) {
        const tray = obj(v);
        const id = int(tray.id, -1);
        if (id !== EXT_LEFT && id !== EXT_RIGHT) continue;
        const type = str(tray.tray_type);
        out.push({
            trayId: id,
            label: trayLabel(id, dual),
            amsId: id,
            slot: 0,
            type,
            colour: normHex(str(tray.tray_color)),
            profileId: str(tray.tray_info_idx),
            name: str(tray.tray_sub_brands),
            remain: -1,
            unitType: 'External',
            reach: !dual || id === EXT_RIGHT ? 'right' : 'left',
            empty: type === ''
        });
    }
    return out;
}

/** The trays a filament can print from: loaded ones, in tray id order. */
export function loadedTrays(trays: Tray[]): Tray[] {
    return trays.filter((t) => !t.empty).sort((a, b) => a.trayId - b.trayId);
}

/** What makes two readings of the spools different for printing: tray, material, colour, preset, name. */
export function traySignature(trays: Tray[]): string {
    return trays
        .map((t) => `${t.trayId}:${t.type}:${t.colour}:${t.profileId}:${t.name}`)
        .sort()
        .join('|');
}

/** A packed `cur | target << 16` temperature (device.extruder.info[].temp). */
export function unpackTemp(v: unknown): { current: number; target: number } {
    const n = int(v, 0);
    return { current: n & 0xffff, target: (n >>> 16) & 0xffff };
}

/** The printer's job and temperatures. */
export function readStatus(state: unknown): PrinterStatus {
    const p = report(state);
    const ext = arr(obj(obj(p.device).extruder).info).map(obj);
    const dual = ext.length >= 2;
    let nozzles: NozzleTemp[];
    if (dual) {
        // Firmware extruder 0 is the main (right) nozzle, 1 the deputy (left): show left first.
        nozzles = [...ext]
            .sort((a, b) => int(b.id, 0) - int(a.id, 0))
            .map((e) => ({ name: int(e.id, 0) === 0 ? 'Right' : 'Left', ...unpackTemp(e.temp) }));
    } else {
        nozzles = [{ name: 'Nozzle', current: num(p.nozzle_temper), target: num(p.nozzle_target_temper) }];
    }
    return {
        state: str(p.gcode_state).toUpperCase(),
        percent: Math.max(0, Math.min(100, int(p.mc_percent, 0))),
        remainingMin: Math.max(0, int(p.mc_remaining_time, 0)),
        layer: int(p.layer_num, 0),
        totalLayers: int(p.total_layer_num, 0),
        job: str(p.subtask_name),
        bed: num(p.bed_temper),
        bedTarget: num(p.bed_target_temper),
        nozzles,
        dual
    };
}

/** A gcode_state in plain words. */
export function stateText(state: string): string {
    const words: Record<string, string> = {
        IDLE: 'Idle',
        PREPARE: 'Preparing',
        SLICING: 'Preparing',
        RUNNING: 'Printing',
        PAUSE: 'Paused',
        FINISH: 'Finished',
        FAILED: 'Failed / stopped',
        INIT: 'Starting up',
        OFFLINE: 'Offline'
    };
    return words[state] ?? (state ? state.charAt(0) + state.slice(1).toLowerCase() : 'Unknown');
}

/** The printer is busy with a job (a new print would be refused). */
export function isBusy(state: string): boolean {
    return state === 'RUNNING' || state === 'PAUSE' || state === 'PREPARE' || state === 'SLICING';
}

/** "1 h 05 min" / "12 min". */
export function minutesText(min: number): string {
    const m = Math.max(0, Math.round(min));
    if (m < 60) return `${m} min`;
    return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`;
}

/** "2 h 44 min" from seconds ("16 s" under a minute). */
export function secondsText(sec: number): string {
    return sec < 59.5 ? `${Math.max(0, Math.round(sec))} s` : minutesText(sec / 60);
}

/** "42.5 g", or "< 0.1 g" for a trace. */
export function gramsText(g: number): string {
    return g > 0 && g < 0.1 ? '< 0.1 g' : `${g.toFixed(1)} g`;
}
