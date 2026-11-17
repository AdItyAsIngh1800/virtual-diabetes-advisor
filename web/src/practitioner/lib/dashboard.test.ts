import { describe, expect, it } from "vitest";

import type { S } from "@/shared/api/client";
import {
  MODEL_METRICS,
  alertsQuery,
  auditQuery,
  claimLine,
  conversationTimeline,
  handoffName,
  kbApproveProblem,
  kbSortDrafts,
  kbStatusLine,
  needsNote,
  overviewTiles,
  patientCells,
  patientsQuery,
  retrainLine,
  reviewQuery,
  reviewTags,
  sortHandoffs,
  sortPatients,
  whyLimits,
} from "@/practitioner/lib/dashboard";

const cr = (count: number, denominator: number): S["CountRate"] => ({ count, denominator, rate: denominator ? count / denominator : null });

const row = (over: Partial<S["PatientRow"]>): S["PatientRow"] => ({
  user_id: "u",
  first_name: null,
  age_band: null,
  diabetes_status: null,
  last_reading: null,
  limits: { fasting_high: 126, post_meal_high: 180, low: 70 },
  alerts_7d: 0,
  breach_7d: false,
  live_handoff: null,
  risk: null,
  ...over,
});

describe("patient list", () => {
  const rows = [
    row({ first_name: "Ravi", age_band: "60-69" }),
    row({
      first_name: "Asha",
      age_band: "50-59",
      alerts_7d: 3,
      risk: { tier: "moderate", score: 0.4, flagged: false, model_version: "v1", assessed_at: "2026-11-17T00:00:00Z" },
      last_reading: { band_state: "breach_low", context: "fasting", measured_at: "2026-11-17T09:00:00Z", value_mg_dl: 50 },
    }),
    row({
      first_name: "Bina",
      age_band: "20-29",
      alerts_7d: 1,
      risk: { tier: "high", score: 0.7, flagged: true, model_version: "v1", assessed_at: "2026-11-17T00:00:00Z" },
      last_reading: { band_state: "normal", context: "post_meal", measured_at: "2026-11-17T10:00:00Z", value_mg_dl: 110 },
    }),
  ];
  const order = (key: Parameters<typeof sortPatients>[1], desc = false) => sortPatients(rows, key, desc).map((r) => r.first_name);

  it("sorts like the classic page, missing values last", () => {
    expect(order("name")).toEqual(["Asha", "Bina", "Ravi"]);
    expect(order("name", true)).toEqual(["Ravi", "Bina", "Asha"]);
    expect(order("state")).toEqual(["Asha", "Bina", "Ravi"]);
    expect(order("state", true)).toEqual(["Bina", "Asha", "Ravi"]);
    expect(order("alerts")).toEqual(["Asha", "Bina", "Ravi"]);
    expect(order("risk")).toEqual(["Bina", "Asha", "Ravi"]);
    expect(order("last")).toEqual(["Bina", "Asha", "Ravi"]);
  });

  it("builds cells from named fields only", () => {
    const leaky = { ...rows[1], email: "asha@example.com", phone: "+911234567890" } as S["PatientRow"];
    const cells = patientCells(leaky);
    expect(JSON.stringify(cells)).not.toMatch(/example\.com|\+91/);
    expect(cells.name).toBe("Asha");
    expect(cells.limits).toBe("126 / 180 / 70");
    expect(cells.risk).toBe("Moderate");
  });

  it("builds list URLs with only the filters that are set", () => {
    expect(patientsQuery({ band_state: "", breach_7d: "true", live_handoff: "" })).toBe("/api/dashboard/patients?limit=100&breach_7d=true");
    expect(alertsQuery({ state: "breach_high", escalated: "", delivery_status: "", since: "", until: "" }, "c1")).toBe(
      "/api/dashboard/alerts?limit=25&cursor=c1&state=breach_high",
    );
    // Local days, both included: until is the next local midnight.
    const url = alertsQuery({ state: "", escalated: "", delivery_status: "", since: "2026-11-10", until: "2026-11-10" });
    expect(url).toContain(`since=${encodeURIComponent(new Date("2026-11-10T00:00:00").toISOString())}`);
    expect(url).toContain(`until=${encodeURIComponent(new Date("2026-11-11T00:00:00").toISOString())}`);
    expect(auditQuery("", "")).toBe("/api/practitioner/audit/summary");
    expect(reviewQuery("unreviewed")).toBe("/api/practitioner/turns?filter=unreviewed&limit=25");
    expect(reviewQuery("all", "c2", 10, "p-1")).toBe("/api/practitioner/turns?filter=all&limit=10&cursor=c2&patient_id=p-1");
  });
});

describe("overview", () => {
  it("shows every rate as x of y", () => {
    const d = { total: 5, queued: cr(0, 4), sent: cr(1, 4), opened: cr(2, 4), failed: cr(1, 4), expired: cr(0, 4), suppressed: cr(1, 5), delivered: cr(3, 4) };
    const tiles = overviewTiles({
      active_patients: 4, readings: 9, pre_breach_alerts: 2, breaches: 1, below_54_readings: 0, open_handoffs: 1, claimed_handoffs: 2,
      delivery: d, last_batch_run: null,
      readings_window: { since: "2026-11-17T00:00:00Z", until: "2026-11-18T00:00:00Z" },
      alerts_window: { since: "2026-11-17T00:00:00Z", until: "2026-11-18T00:00:00Z" },
    });
    const delivered = tiles.find((t) => t.key === "delivery");
    expect(delivered?.value).toBe("3 of 4");
    expect(delivered?.detail).toBe("75% sent or opened");
    expect(tiles.find((t) => t.key === "breach")?.urgent).toBe(true);
  });
});

describe("why is this the limit", () => {
  it("takes the latest step of each kind and shows the 0 risk adjustment", () => {
    const base = {
      direction: "upper" as const, model_version: "t", reason: "batch" as const, source_citation: "ADA 2026 s6",
      hard_min_mg_dl: 100, hard_max_mg_dl: 140, demo_adj_mg_dl: 5, risk_adj_mg_dl: 0, trend_adj_mg_dl: 0,
    };
    const older = { ...base, id: "a", limit_kind: "fasting_high" as const, effective_from: "2026-11-01T00:00:00Z", base_mg_dl: 130, unclamped_mg_dl: 135, value_mg_dl: 135 };
    const newer = { ...base, id: "b", limit_kind: "fasting_high" as const, effective_from: "2026-11-05T00:00:00Z", base_mg_dl: 130, trend_adj_mg_dl: 10, unclamped_mg_dl: 145, value_mg_dl: 140 };
    const rows = whyLimits([newer, older]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ label: "Fasting", base: 130, demo: 5, risk: 0, trend: 10, sum: 145, value: 140, clamped: true, reason: "Batch recompute", citation: "ADA 2026 s6" });
  });
});

describe("handoffs", () => {
  const h = (id: string, status: S["HandoffSummary"]["status"], at: string): S["HandoffSummary"] => ({
    id, status, trigger: "practitioner_request", opened_at: at, last_message_at: null, patient_id: "p", patient_name: "Asha",
    practitioner_id: null, practitioner_name: status === "assigned" ? "Meera" : null, assigned_at: null, resolved_at: null, reason: null,
  });
  it("lists waiting first, then claimed, then resolved", () => {
    const list = [h("r", "resolved", "2026-11-17T10:00:00Z"), h("a", "assigned", "2026-11-17T09:00:00Z"), h("o1", "open", "2026-11-17T08:00:00Z"), h("o2", "open", "2026-11-17T11:00:00Z")];
    expect(sortHandoffs(list).map((x) => x.id)).toEqual(["o2", "o1", "a", "r"]);
  });
  it("shows only the patient's first name", () => {
    expect(handoffName({ ...h("a", "open", "2026-11-17T09:00:00Z"), patient_name: "Meera Iyer" })).toBe("Meera");
    expect(handoffName({ ...h("a", "open", "2026-11-17T09:00:00Z"), patient_name: null, patient_id: "0123456789abcdef" })).toBe("Patient 01234567");
  });
  it("says who has the chat", () => {
    expect(claimLine(h("a", "assigned", "2026-11-17T09:00:00Z"), "Meera")).toMatch(/^Claimed by you/);
    expect(claimLine(h("a", "assigned", "2026-11-17T09:00:00Z"), "Ravi")).toMatch(/^Claimed by Meera/);
    expect(claimLine(h("o", "open", "2026-11-17T09:00:00Z"), null)).toMatch(/^Not claimed yet/);
  });
});

describe("conversation tab", () => {
  it("merges answers and handoffs newest first", () => {
    const turn = (id: string, at: string) => ({ turn_id: id, created_at: at }) as S["ReviewableTurn"];
    const handoff = (id: string, opened: string, last: string | null) => ({ id, opened_at: opened, last_message_at: last }) as S["HandoffSummary"];
    const items = conversationTimeline(
      [turn("t1", "2026-11-17T09:00:00Z"), turn("t2", "2026-11-17T11:00:00Z")],
      [handoff("h1", "2026-11-17T08:00:00Z", "2026-11-17T10:00:00Z"), handoff("h2", "2026-11-17T12:00:00Z", null)],
    );
    expect(items.map((i) => (i.kind === "answer" ? i.turn.turn_id : i.handoff.id))).toEqual(["h2", "t2", "h1", "t1"]);
  });
});

describe("review", () => {
  it("needs a note unless Correct", () => {
    expect(needsNote("correct")).toBe(false);
    expect(["incorrect", "unsafe", "needs_kb_update"].every((v) => needsNote(v as "unsafe"))).toBe(true);
  });
  it("tags a turn, current verdict last", () => {
    const tags = reviewTags({
      turn_id: "t", created_at: "2026-11-17T09:00:00Z", answer: "x", question: "q", escalated: true, unanswered: false, low_confidence: true,
      retrieval_confidence: 0.41, spoken: true, intent: null, kb_version: null, model: null, model_version: null, patient_first_name: "Asha",
      review_count: 1, sources: [],
      current_review: { id: "r", turn_id: "t", verdict: "unsafe", note: "n", reviewer_id: "x", reviewer_name: "Meera", created_at: "2026-11-17T09:00:00Z" },
    });
    expect(tags.map((t) => t.text)).toEqual(["Escalated", "Low confidence 0.41", "Voice", "Unsafe"]);
    expect(tags.at(-1)?.urgent).toBe(true);
  });
});

describe("knowledge base", () => {
  it("needs body and citation to approve", () => {
    expect(kbApproveProblem({ body: " ", source_citation: null })).toBe("Add the entry text and a source citation before approving.");
    expect(kbApproveProblem({ body: "x", source_citation: "" })).toBe("Add a source citation before approving.");
    expect(kbApproveProblem({ body: "x", source_citation: "ADA" })).toBeNull();
  });
  it("puts priority drafts first, otherwise keeps the API order", () => {
    const d = (id: string, priority: boolean) => ({ id, priority }) as S["KbEntryView"];
    expect(kbSortDrafts([d("a", false), d("b", true), d("c", false), d("e", true)]).map((x) => x.id)).toEqual(["b", "e", "a", "c"]);
  });
  it("says how much was approved by the seed account", () => {
    const line = kbStatusLine({ approved: 12, approved_by_seed_account: cr(12, 12), approved_by_others: cr(0, 12), drafts: 1, priority_drafts: cr(1, 1) });
    expect(line).toBe("12 of 12 approved entries were approved by the seed account, not yet reviewed by a practitioner. 1 draft awaiting approval (1 from answers marked unsafe).");
  });
});

describe("model card", () => {
  it("lists precision, recall and F1 before ROC-AUC", () => {
    const keys = MODEL_METRICS.map(([k]) => k);
    expect(keys.indexOf("roc_auc")).toBeGreaterThan(keys.indexOf("precision"));
    expect(keys.indexOf("roc_auc")).toBeGreaterThan(keys.indexOf("recall"));
    expect(keys.indexOf("roc_auc")).toBeGreaterThan(keys.indexOf("f1"));
  });
  it("describes the last retrain", () => {
    expect(retrainLine(null)).toBe("Last retrain: none yet. Retraining is manual (python -m app.jobs.retrain).");
    expect(retrainLine({ run_id: "r", status: "succeeded", decision: "kept", error: null, started_at: "x", finished_at: null, triggered_by: "manual", report: null })).toBe("Last retrain: current model kept.");
  });
});
