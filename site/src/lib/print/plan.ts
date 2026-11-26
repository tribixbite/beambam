/**
 * Print settings -> the `X-Slice-Options` header, and the filament -> spool plan beambam
 * returns from `POST /print/plan` (types plus the small pure helpers the mapping panel uses).
 */
import { deltaE } from './colour';
import type { Tray } from './printer';

/** Support generation: Bambu's "where needed", "from the build plate only", or none. */
export type SupportMode = 'auto' | 'plate' | 'off';
/** Brim: Bambu's automatic brim, an outer brim only, or none. */
export type BrimMode = 'auto_brim' | 'outer_only' | 'no_brim';

/** Bambu Studio's build plates (`curr_bed_type`), as beambam's `bed_type` slice option takes them. */
export const BED_TYPES = ['Cool Plate', 'Engineering Plate', 'High Temp Plate', 'Textured PEI Plate', 'Supertack Plate'] as const;
export type BedType = (typeof BED_TYPES)[number];

/** A plate name in the words its maker uses ("SuperTack plate", "Textured PEI plate"). */
export function plateText(name: string): string {
    return name.replace(/^Supertack/, 'SuperTack').replace(/ Plate$/, ' plate');
}

/** What the user chose on the settings card; null = the project's own value (not sent). */
export interface PrintSettings {
    supports: SupportMode | null;
    layerHeight: number | null;
    walls: number | null;
    infill: number | null;
    brim: BrimMode | null;
    /** The build plate to slice for. */
    bed: BedType | null;
}

/** Every setting left to the project. */
export const PROJECT_SETTINGS: PrintSettings = {
    supports: null,
    layerHeight: null,
    walls: null,
    infill: null,
    brim: null,
    bed: null
};

/** Layer heights Bambu offers for a 0.4 mm nozzle (the daemon accepts 0.08..0.28). */
export const LAYER_HEIGHTS = [0.08, 0.12, 0.16, 0.2, 0.24, 0.28] as const;
export const WALL_COUNTS = [1, 2, 3, 4, 5, 6] as const;
export const INFILL_STEPS = [0, 5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100] as const;

/** The spool a mesh file's single filament prints from (its preset and material go to the slicer). */
export interface MeshFilament {
    profileId: string;
    type: string;
    colour: string;
}

/** The `X-Slice-Options` body (beambam/slice_options.py `SliceOptions.from_dict`). */
export interface SliceOptions {
    layer_height?: number;
    wall_loops?: number;
    infill?: number;
    supports?: SupportMode;
    brim?: BrimMode;
    nozzles?: number[];
    bed_type?: BedType;
    /** Per filament: the spool it prints from; a project takes its preset and colour (beambam "project-sync"). */
    spools?: SpoolOption[];
    filament_ids?: string[];
    filament_types?: string[];
}

/** One filament's spool as the `spools` slice option takes it (null id / type: keep the project's preset). */
export interface SpoolOption {
    id: string | null;
    type: string | null;
    colour: string;
}

/** True when any print setting differs from the project's. */
export function hasSettings(s: PrintSettings): boolean {
    return Object.values(s).some((v) => v !== null);
}

/**
 * The options a slice sends: only the settings the user changed, the nozzle choice when one
 * was made, and for a mesh file the spool's preset and material (the colour is the spool's own on
 * the printer, so it is not sent). Null when there is
 * nothing to send (the header is then left out, which also works with an older beambam).
 */
export function buildSliceOptions(
    settings: PrintSettings,
    mesh: MeshFilament | null = null,
    nozzles: number[] | null = null,
    spools: SpoolOption[] | null = null
): SliceOptions | null {
    const o: SliceOptions = {};
    if (settings.layerHeight !== null) o.layer_height = settings.layerHeight;
    if (settings.walls !== null) o.wall_loops = settings.walls;
    if (settings.infill !== null) o.infill = settings.infill;
    if (settings.supports !== null) o.supports = settings.supports;
    if (settings.brim !== null) o.brim = settings.brim;
    if (settings.bed !== null) o.bed_type = settings.bed;
    if (nozzles && nozzles.length) o.nozzles = [...nozzles];
    if (spools && spools.length) o.spools = spools.map((s) => ({ ...s }));
    if (mesh) {
        // An empty preset id would be refused; the material alone picks Bambu's generic profile.
        if (mesh.profileId) o.filament_ids = [mesh.profileId];
        if (mesh.type) o.filament_types = [mesh.type];
    }
    return Object.keys(o).length ? o : null;
}

/**
 * Compact JSON for the header (the daemon caps it at 4096 bytes). Non-ASCII (a spool type or preset name the
 * printer reports) is escaped: a header value with other characters makes fetch throw.
 */
export function optionsHeader(o: SliceOptions): string {
    return JSON.stringify(o).replace(/[\u007f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

/** One filament row of beambam's plan. */
export interface PlanFilament {
    /** 1-based filament number in the print file. */
    filament: number;
    type: string;
    colour: string;
    /** The nozzle it was sliced for: "left" | "right" | "both" | "-" (one nozzle). */
    nozzle: string;
    tray: { tray_id: number; label: string; type: string; colour: string; profile_id?: string } | null;
    errors: string[];
    warnings: string[];
}

/** `POST /print/plan`'s answer (errors refuse the print; warnings need the user's OK). */
export interface PrintPlan {
    filaments: PlanFilament[];
    errors: string[];
    warnings: string[];
    notes: string[];
    /** A hash of what this plan shows; POST /print with it starts only this plan (beambam ≥ 1.9). */
    digest?: string;
    /** The plate the file was sliced for and the plate on the printer (beambam with "plate-check"). */
    bed?: { file: string | null; printer: string | null; printer_marker: string | null };
    ok?: boolean;
    needs_confirmation?: boolean;
}

/** The nozzle number (1 = left, 2 = right) a plan names, or null ("-": one nozzle; "both": either). */
export function nozzleNumber(name: string): 1 | 2 | null {
    return name === 'left' ? 1 : name === 'right' ? 2 : null;
}

/**
 * Every filament's nozzle (index 0 = filament 1) as the plan has it, with `filament` moved to
 * `to`. Filaments the plan does not list (unused) and "both" print on the left, Bambu's default.
 */
export function nozzlesWith(plan: PrintPlan, filament: number, to: 1 | 2): number[] {
    const n = Math.max(filament, ...plan.filaments.map((f) => f.filament));
    const out = new Array<number>(n).fill(1);
    for (const f of plan.filaments) out[f.filament - 1] = nozzleNumber(f.nozzle) ?? 1;
    out[filament - 1] = to;
    return out;
}

/** The plan has a filament on either nozzle of a two-nozzle printer (the Left|Right switch applies). */
export function isTwoNozzlePlan(plan: PrintPlan): boolean {
    return plan.filaments.some((f) => nozzleNumber(f.nozzle) !== null);
}

/**
 * The trays beambam assigned, filament-indexed (index 0 = filament 1, -1 = a filament the plate does not use):
 * the form beambam's `index_slots` reads unambiguously (a list with -1 in it is always filament-indexed, and one
 * without is the same in both of its readings). Null when a used filament has no tray (it cannot print yet).
 */
export function slotsFromPlan(plan: PrintPlan): number[] | null {
    if (!plan.filaments.length || plan.filaments.some((f) => typeof f.tray?.tray_id !== 'number')) return null;
    return filamentIndexed(plan, null);
}

/** Filament-indexed trays from the plan (`slots` taking precedence where given), -1 for no tray. */
function filamentIndexed(plan: PrintPlan, slots: number[] | null): number[] {
    const n = Math.max(0, ...plan.filaments.map((f) => f.filament), slots?.length ?? 0);
    const out = new Array<number>(n).fill(-1);
    for (const f of plan.filaments) out[f.filament - 1] = f.tray?.tray_id ?? -1;
    slots?.forEach((s, i) => { if (s >= 0) out[i] = s; });
    return out;
}

/** `slots` (filament-indexed) with the filament of plan row `index` moved to tray `trayId`. */
export function withSlot(plan: PrintPlan, slots: number[] | null, index: number, trayId: number): number[] {
    const out = filamentIndexed(plan, slots);
    out[plan.filaments[index].filament - 1] = trayId;
    return out;
}

/** A spool choice in a picker, ranked against a filament colour. */
export interface SpoolChoice {
    trayId: number;
    label: string;
    detail: string;
    /** "" for a tray that is no longer loaded. */
    colour: string;
    /** ΔE to the filament colour; Infinity when there is no colour to compare. */
    de: number;
    /** The choice cannot be printed from (shown, not pickable). */
    unavailable: boolean;
}

/** "PLA · PLA Basic · 82 %" */
export function trayDetail(t: Tray): string {
    return [t.type, t.name, t.remain >= 0 ? `${t.remain} %` : ''].filter(Boolean).join(' · ');
}

/**
 * Picker choices for one filament: the loaded spools, spools of the filament's own material
 * first (another material is a beambam error), each group closest colour first; plus the
 * filament's current tray when it is no longer loaded (so the row still says what it holds).
 */
export function spoolChoices(trays: Tray[], colour: string, current: number | null, type = ''): SpoolChoice[] {
    const want = type.trim().toUpperCase();
    const other = (t: Tray): number => (want && t.type.toUpperCase() !== want ? 1 : 0);
    const ranked = trays.filter((t) => !t.empty);
    const otherOf = new Map(ranked.map((t) => [t.trayId, other(t)]));
    const out: SpoolChoice[] = ranked
        .map((t) => ({
            trayId: t.trayId,
            label: t.label,
            detail: trayDetail(t),
            colour: t.colour,
            de: t.colour && colour ? deltaE(t.colour, colour) : Infinity,
            unavailable: false
        }));
    out.sort((a, b) => (otherOf.get(a.trayId) ?? 0) - (otherOf.get(b.trayId) ?? 0) || a.de - b.de || a.trayId - b.trayId);
    if (current !== null && current >= 0 && !out.some((c) => c.trayId === current)) {
        const t = trays.find((x) => x.trayId === current);
        out.push({
            trayId: current,
            label: t?.label ?? `slot ${current}`,
            detail: 'not loaded now',
            colour: '',
            de: Infinity,
            unavailable: true
        });
    }
    return out;
}

/** The loaded tray closest in colour to `colour` (preferring `type` when given), or null. */
export function closestTray(trays: Tray[], colour: string, type = ''): Tray | null {
    const loaded = trays.filter((t) => !t.empty);
    const same = type ? loaded.filter((t) => t.type.toUpperCase() === type.toUpperCase()) : [];
    const pool = same.length ? same : loaded;
    let best: Tray | null = null;
    let bestDe = Infinity;
    for (const t of pool) {
        const de = t.colour && colour ? deltaE(t.colour, colour) : 1e6;
        if (de < bestDe) {
            best = t;
            bestDe = de;
        }
    }
    return best;
}

/** The supports choices as the settings card offers them (null = the project's own). */
export const SUPPORT_MODES_UI: { value: SupportMode | null; label: string }[] = [
    { value: null, label: "Project's" },
    { value: 'auto', label: 'Where needed' },
    { value: 'plate', label: 'From plate only' },
    { value: 'off', label: 'Off' }
];

/** The brim choices as the settings card offers them (null = the project's own). */
export const BRIM_MODES_UI: { value: BrimMode | null; label: string }[] = [
    { value: null, label: "Project's" },
    { value: 'auto_brim', label: 'Auto' },
    { value: 'outer_only', label: 'Outside only' },
    { value: 'no_brim', label: 'None' }
];

/**
 * Filaments whose sliced nozzle (the plan's) is not the one asked for in `requested`
 * (1 = left, 2 = right, index 0 = filament 1): "filament 1 (asked left, sliced right)".
 */
export function nozzleMismatches(plan: PrintPlan, requested: number[] | null): string[] {
    if (!requested) return [];
    const name = (n: number): string => (n === 1 ? 'left' : 'right');
    const out: string[] = [];
    for (const f of plan.filaments) {
        const got = nozzleNumber(f.nozzle);
        const want = requested[f.filament - 1];
        if (got !== null && want && got !== want) out.push(`filament ${f.filament} (asked ${name(want)}, sliced ${name(got)})`);
    }
    return out;
}

/** ΔE under which a project colour and a spool's count as the same (spool readings vary; pokefit uses 6 too). */
export const SAME_COLOUR = 6;

/** One project filament against the spool beambam's plan prints it from. */
export interface SyncRow {
    filament: number;
    project: { type: string; colour: string };
    spool: { label: string; type: string; colour: string } | null;
    same: boolean;
}

/**
 * The print file's filaments (the project's filament list as sliced) against the spools the plan
 * prints them from: a row per filament the plate uses; `same` = same material and colour within
 * SAME_COLOUR. After a slice with the spools' filaments every row is the same.
 */
export function projectVsSpools(plan: PrintPlan): { rows: SyncRow[]; differs: boolean } {
    const rows = plan.filaments.map((f): SyncRow => {
        const p = { type: f.type, colour: f.colour };
        const t = f.tray;
        const same = !!t && !!t.type && t.type.toUpperCase() === p.type.toUpperCase() &&
            !!t.colour && !!p.colour && deltaE(t.colour, p.colour) <= SAME_COLOUR;
        return { filament: f.filament, project: { type: p.type, colour: p.colour }, spool: t ? { label: t.label, type: t.type, colour: t.colour } : null, same };
    });
    return { rows, differs: rows.some((r) => !r.same) };
}

/**
 * The `spools` option that gives the project the plan's spools: filament i the preset, material
 * and colour of the spool it prints from; a filament the plate does not use keeps its own
 * (null id / type, its colour).
 */
export function spoolsFromPlan(filaments: { type: string; colour: string }[], plan: PrintPlan): SpoolOption[] {
    const n = Math.max(filaments.length, ...plan.filaments.map((f) => f.filament));
    const out: SpoolOption[] = [];
    for (let i = 0; i < n; i++) {
        const f = plan.filaments.find((x) => x.filament === i + 1);
        const t = f?.tray;
        out.push(t && t.colour
            ? { id: t.profile_id || null, type: t.type || null, colour: t.colour.slice(0, 7).toUpperCase() }
            : { id: null, type: null, colour: (filaments[i]?.colour || '#808080').slice(0, 7).toUpperCase() });
    }
    return out;
}
