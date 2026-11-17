import { describe, expect, it } from "vitest";

import type { S } from "@/shared/api/client";
import { bandEdges, bandState, chartModel, dayTicks, niceTicks, scale, stepSegments, yDomain } from "@/practitioner/lib/chart";

/** xs[i], failing the test if it is missing. */
function at<T>(xs: readonly T[], i: number): T {
  const x = xs[i];
  if (x === undefined) throw new Error(`no item ${i}`);
  return x;
}

const LIMITS = { low: 70, fasting_high: 130, post_meal_high: 180 };
const DAY = 86_400_000;

describe("pre-breach band (mirrors classify_reading in thresholds.py)", () => {
  it("classifies fasting readings at every boundary (5 % band)", () => {
    const values = [69.9, 70.0, 70.1, 73.4, 73.5, 73.6, 100.0, 123.4, 123.5, 129.9, 130.0, 130.1];
    expect(values.map((v) => bandState(v, "fasting", LIMITS, 0.05))).toEqual([
      "breach_low", "breach_low", "pre_breach_low", "pre_breach_low", "pre_breach_low", "normal",
      "normal", "normal", "pre_breach_high", "pre_breach_high", "breach_high", "breach_high",
    ]);
  });

  it("uses the post-meal limit after a meal and no upper limit for random readings", () => {
    expect([170.9, 171.0, 179.9, 180.0].map((v) => bandState(v, "post_meal", LIMITS, 0.05))).toEqual([
      "normal", "pre_breach_high", "pre_breach_high", "breach_high",
    ]);
    expect([54, 73.5, 73.6, 400].map((v) => bandState(v, "random", LIMITS, 0.05))).toEqual([
      "breach_low", "pre_breach_low", "normal", "normal",
    ]);
  });

  it("widens with the band and is null when a needed limit is missing", () => {
    expect(bandState(117.1, "fasting", LIMITS, 0.1)).toBe("pre_breach_high");
    expect(bandState(116.9, "fasting", LIMITS, 0.1)).toBe("normal");
    expect(bandState(100, "fasting", { low: 70, fasting_high: null, post_meal_high: null }, 0.05)).toBeNull();
    expect(bandState(100, "random", { low: 70, fasting_high: null, post_meal_high: null }, 0.05)).toBe("normal");
  });

  it("puts the band below an upper limit and above a lower one", () => {
    const [lo, hi] = bandEdges(130, "upper", 0.05);
    expect(lo).toBeCloseTo(123.5);
    expect(hi).toBe(130);
    const [lo2, hi2] = bandEdges(70, "lower", 0.05);
    expect(lo2).toBe(70);
    expect(hi2).toBeCloseTo(73.5);
  });
});

describe("scaling", () => {
  it("is linear, flips for SVG y, and maps an empty domain to the middle", () => {
    expect(scale(5, 0, 10, 0, 100)).toBe(50);
    expect(scale(0, 0, 10, 200, 0)).toBe(200);
    expect(scale(10, 0, 10, 200, 0)).toBe(0);
    expect(scale(3, 3, 3, 0, 100)).toBe(50);
  });

  it("picks round ticks", () => {
    expect(niceTicks(40, 260, 5)).toEqual([50, 100, 150, 200, 250]);
    expect(niceTicks(60, 140, 5)).toEqual([60, 80, 100, 120, 140]);
    expect(niceTicks(0, 1, 5)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
  });

  it("keeps readings, limits and their bands in view, on whole tens", () => {
    const [lo, hi] = yDomain([50, 128], [70, 130, 180], 0.05);
    expect(lo).toBeLessThanOrEqual(40);
    expect(hi).toBeGreaterThanOrEqual(190);
    expect(lo % 10).toBe(0);
    expect(hi % 10).toBe(0);
    expect(yDomain([], [], 0.05)).toEqual([40, 250]);
  });

  it("puts day ticks on local midnights inside the window", () => {
    const t0 = new Date(2026, 10, 1, 9).getTime();
    const ticks = dayTicks(t0, t0 + 10 * DAY, 20);
    expect(ticks).toHaveLength(10);
    for (const t of ticks) expect(new Date(t).getHours()).toBe(0);
    expect(dayTicks(t0, t0 + 30 * DAY, 5).length).toBeLessThanOrEqual(6);
  });
});

const step = (kind: S["ThresholdStep"]["limit_kind"], at: string, value: number): S["ThresholdStep"] => ({
  id: `${kind}-${at}`,
  limit_kind: kind,
  direction: kind === "low" ? "lower" : "upper",
  effective_from: at,
  value_mg_dl: value,
  base_mg_dl: value,
  demo_adj_mg_dl: 0,
  risk_adj_mg_dl: 0,
  trend_adj_mg_dl: 0,
  unclamped_mg_dl: value,
  hard_min_mg_dl: 0,
  hard_max_mg_dl: 400,
  model_version: "t",
  reason: "initial",
  source_citation: "ADA",
});

const STEPS = [
  step("fasting_high", "2026-11-03T00:00:00Z", 130),
  step("fasting_high", "2026-11-08T00:00:00Z", 120),
  step("low", "2026-10-01T00:00:00Z", 70),
];

describe("limit step lines", () => {
  const t0 = Date.parse("2026-11-01T00:00:00Z");

  it("cover the window and dash the stretch before the first limit", () => {
    expect(stepSegments(STEPS, "fasting_high", t0, t0 + 10 * DAY)).toEqual([
      { from: t0, to: t0 + 2 * DAY, value: 130, beforeFirst: true },
      { from: t0 + 2 * DAY, to: t0 + 7 * DAY, value: 130, beforeFirst: false },
      { from: t0 + 7 * DAY, to: t0 + 10 * DAY, value: 120, beforeFirst: false },
    ]);
  });

  it("start a limit older than the window at its left edge", () => {
    expect(stepSegments(STEPS, "low", t0, t0 + DAY)).toEqual([{ from: t0, to: t0 + DAY, value: 70, beforeFirst: false }]);
    expect(stepSegments([], "low", t0, t0 + DAY)).toEqual([]);
  });

  it("build the chart model with bands, ringed breaches and the dashed flag", () => {
    const detail: S["PatientDetail"] = {
      user_id: "u",
      first_name: "Asha",
      age_band: null,
      diabetes_status: "diabetes",
      window: { since: "2026-11-01T00:00:00Z", until: "2026-11-11T00:00:00Z" },
      pre_breach_band: 0.05,
      readings: [
        { id: "r1", measured_at: "2026-11-02T00:00:00Z", value_mg_dl: 135, context: "fasting", band_state: "breach_high", limit_kind: "fasting_high", before_first_limit: true },
        { id: "r2", measured_at: "2026-11-06T00:00:00Z", value_mg_dl: 100, context: "post_meal", band_state: "normal", limit_kind: null, before_first_limit: false },
      ],
      thresholds: STEPS,
      risk: null,
      advisories: [],
    };
    const m = chartModel(detail);
    expect(m.limits.map((l) => l.kind)).toEqual(["fasting_high", "low"]);
    const fasting = at(at(m.limits, 0).segments, 1);
    expect(fasting.bandLo).toBeCloseTo(123.5);
    expect(fasting.bandHi).toBe(130);
    const low = at(at(m.limits, 1).segments, 0);
    expect(low.bandLo).toBe(70);
    expect(low.bandHi).toBeCloseTo(73.5);
    expect(m.points.map((p) => p.breach)).toEqual([true, false]);
    expect(m.beforeFirst).toBe(true);
    expect(m.domain[0]).toBeLessThan(70);
    expect(m.domain[1]).toBeGreaterThan(135);
    expect(m.yTicks[0]).toBeGreaterThanOrEqual(m.domain[0]);
  });
});
