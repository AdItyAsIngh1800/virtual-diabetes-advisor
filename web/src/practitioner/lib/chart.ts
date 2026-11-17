/**
 * Glucose chart maths (practitioner patient detail), ported from the classic
 * common.js with the same behaviour and tested in chart.test.ts: the pre-breach band
 * edges, the limit step segments (with the dashed "before first limit" stretch), the
 * y domain and round ticks, and the band state of a reading. Recharts draws what
 * these return; nothing here touches the DOM.
 */
import type { S } from "@/shared/api/client";
import type { BandState, LimitKind } from "@/shared/lib/labels";

export type Direction = "upper" | "lower";

/** The three limits in display order, with the side their band sits on. */
export const CHART_LIMITS: readonly (readonly [LimitKind, Direction])[] = [
  ["fasting_high", "upper"],
  ["post_meal_high", "upper"],
  ["low", "lower"],
];

/** [lower, upper] mg/dL of the pre-breach band of a limit (band 0.05 = 5 %). */
export function bandEdges(limit: number, direction: Direction, band: number): [number, number] {
  return direction === "upper" ? [limit * (1 - band), limit] : [limit, limit * (1 + band)];
}

// Mirrors classify_reading in app/services/thresholds.py. Python compares exact
// decimals; doubles are not exact (130 * 0.95 = 123.49999...), so values (one decimal
// place) and the band (up to four) are compared as integers.
const TENTHS = 10;
const BAND_UNITS = 10_000;
const asTenths = (v: number): number => Math.round(v * TENTHS);

/** Band state of a reading, or null if a limit it needs is missing. */
export function bandState(
  value: number,
  context: "fasting" | "post_meal" | "random",
  limits: { fasting_high: number | null; post_meal_high: number | null; low: number | null },
  band: number,
): BandState | null {
  const upperKind = context === "fasting" ? "fasting_high" : context === "post_meal" ? "post_meal_high" : null;
  const low = limits.low;
  const upper = upperKind ? limits[upperKind] : null;
  if (low === null || (upperKind && upper === null)) return null;
  const b = Math.round(band * BAND_UNITS);
  const x = asTenths(value) * BAND_UNITS;
  const L = asTenths(low);
  if (x <= L * BAND_UNITS) return "breach_low";
  if (upper !== null && x >= asTenths(upper) * BAND_UNITS) return "breach_high";
  if (x <= L * (BAND_UNITS + b)) return "pre_breach_low";
  if (upper !== null && x >= asTenths(upper) * (BAND_UNITS - b)) return "pre_breach_high";
  return "normal";
}

/** Map v from [d0, d1] onto [r0, r1] (linear). A zero-width domain maps to the middle. */
export function scale(v: number, d0: number, d1: number, r0: number, r1: number): number {
  if (d1 === d0) return (r0 + r1) / 2;
  return r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);
}

/** Round ticks covering [min, max]: steps of 1, 2, 2.5 or 5 x 10^n, about `count` of them. */
export function niceTicks(min: number, max: number, count = 5): number[] {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const ticks: number[] = [];
  for (let t = Math.ceil(min / step) * step; t <= max + step / 1e6; t += step) ticks.push(Math.round(t * 1e6) / 1e6);
  return ticks;
}

/** One horizontal stretch of a limit. Times are epoch ms. */
export interface Segment {
  from: number;
  to: number;
  value: number;
  /** Before the first stored limit: drawn dashed (the batch judged that backlog by it). */
  beforeFirst: boolean;
}

/** Step segments of one limit over [t0, t1]. */
export function stepSegments(steps: readonly S["ThresholdStep"][], kind: LimitKind, t0: number, t1: number): Segment[] {
  const rows = steps
    .filter((s) => s.limit_kind === kind)
    .map((s) => ({ at: Date.parse(s.effective_from), value: s.value_mg_dl }))
    .sort((a, b) => a.at - b.at);
  const first = rows[0];
  if (!first) return [];
  const out: Segment[] = [];
  if (first.at > t0) out.push({ from: t0, to: Math.min(first.at, t1), value: first.value, beforeFirst: true });
  rows.forEach((r, i) => {
    const from = Math.max(r.at, t0);
    const next = rows[i + 1];
    const to = next ? Math.min(next.at, t1) : t1;
    if (to > from) out.push({ from, to, value: r.value, beforeFirst: false });
  });
  return out;
}

/** The y domain: every reading and limit (with its band) in view, padded, whole tens. */
export function yDomain(values: readonly number[], limitValues: readonly number[], band: number): [number, number] {
  const all = [...values, ...limitValues, ...limitValues.map((v) => v * (1 - band))];
  if (!all.length) return [40, 250];
  const lo = Math.min(...all);
  const hi = Math.max(...all);
  const pad = Math.max(10, (hi - lo) * 0.08);
  return [Math.max(0, Math.floor((lo - pad) / 10) * 10), Math.ceil((hi + pad) / 10) * 10];
}

/** One limit as drawn: its step segments and the band rectangle of each. */
export interface LimitSeries {
  kind: LimitKind;
  direction: Direction;
  segments: (Segment & { bandLo: number; bandHi: number })[];
}

export interface ChartPoint {
  id: string;
  t: number;
  value: number;
  context: "fasting" | "post_meal" | "random";
  state: BandState | null;
  breach: boolean;
  beforeFirst: boolean;
  measuredAt: string;
}

export interface ChartModel {
  t0: number;
  t1: number;
  band: number;
  domain: [number, number];
  yTicks: number[];
  xTicks: number[];
  limits: LimitSeries[];
  points: ChartPoint[];
  beforeFirst: boolean;
}

const DAY = 86_400_000;

/** Daily x ticks (local midnights) inside [t0, t1], thinned to about `max` of them. */
export function dayTicks(t0: number, t1: number, max: number): number[] {
  const days = Math.max(1, Math.round((t1 - t0) / DAY));
  const every = Math.max(1, Math.ceil(days / Math.max(2, max)));
  const first = new Date(t0);
  first.setHours(0, 0, 0, 0);
  const out: number[] = [];
  for (let d = 1; ; d += every) {
    const t = new Date(first.getFullYear(), first.getMonth(), first.getDate() + d).getTime();
    if (t > t1) break;
    out.push(t);
  }
  return out;
}

/** Everything the chart draws, in data units (Recharts maps them to pixels). */
export function chartModel(detail: S["PatientDetail"], maxXTicks = 7): ChartModel {
  const t0 = Date.parse(detail.window.since);
  const t1 = Date.parse(detail.window.until);
  const band = detail.pre_breach_band;
  const limits: LimitSeries[] = CHART_LIMITS.map(([kind, direction]) => ({
    kind,
    direction,
    segments: stepSegments(detail.thresholds, kind, t0, t1).map((s) => {
      const [bandLo, bandHi] = bandEdges(s.value, direction, band);
      return { ...s, bandLo, bandHi };
    }),
  })).filter((l) => l.segments.length > 0);
  const domain = yDomain(
    detail.readings.map((r) => r.value_mg_dl),
    limits.flatMap((l) => l.segments.map((s) => s.value)),
    band,
  );
  return {
    t0,
    t1,
    band,
    domain,
    yTicks: niceTicks(domain[0], domain[1], 5),
    xTicks: dayTicks(t0, t1, maxXTicks),
    limits,
    points: detail.readings.map((r) => ({
      id: r.id,
      t: Date.parse(r.measured_at),
      value: r.value_mg_dl,
      context: r.context,
      state: r.band_state,
      breach: r.band_state === "breach_high" || r.band_state === "breach_low",
      beforeFirst: r.before_first_limit,
      measuredAt: r.measured_at,
    })),
    beforeFirst: limits.some((l) => l.segments.some((s) => s.beforeFirst)),
  };
}
