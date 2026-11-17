/**
 * Pure helpers for the practitioner app, ported from the classic common.js with the
 * same behaviour (tested in dashboard.test.ts): URLs, sorting, table cells, KPI
 * tiles, the "Why is this the limit?" rows, review and knowledge-base rules, and the
 * audit and model-card wording. Patients are shown by first name only; no helper
 * reads an e-mail, phone number or push endpoint.
 */
import type { S } from "@/shared/api/client";
import { countOf, dayStart, firstName, mgdl, pct, shortWhen, withParams, type CountRate } from "@/shared/lib/format";
import {
  BAND_TONE,
  CONTEXT_LABELS,
  DELIVERY_LABELS,
  HANDOFF_LABELS,
  LIMIT_LABELS,
  REASON_LABELS,
  bandLabel,
  type BandState,
  type LimitKind,
  type Tone,
} from "@/shared/lib/labels";
import { CHART_LIMITS } from "@/practitioner/lib/chart";

export { countOf, pct };

// --- Overview ----------------------------------------------------------------------

export interface Tile {
  key: string;
  label: string;
  value: string;
  detail: string;
  urgent: boolean;
  /** Where the tile links (a filtered list), if anywhere. */
  to?: string;
}

/** KPI tiles of the overview. Every rate says "x of y". */
export function overviewTiles(ov: S["Overview"]): Tile[] {
  const d = ov.delivery;
  return [
    { key: "patients", label: "Active patients", value: String(ov.active_patients), detail: "assigned to you", urgent: false, to: "/patients" },
    { key: "readings", label: "Readings today", value: String(ov.readings), detail: "since midnight", urgent: false },
    { key: "pre", label: "Pre-breach alerts", value: String(ov.pre_breach_alerts), detail: "last 24 h", urgent: false, to: "/alerts" },
    { key: "breach", label: "Breaches", value: String(ov.breaches), detail: "last 24 h", urgent: ov.breaches > 0, to: "/alerts" },
    { key: "hypo", label: "Below 54 mg/dL", value: String(ov.below_54_readings), detail: "last 24 h", urgent: ov.below_54_readings > 0 },
    { key: "open", label: "Handoffs waiting", value: String(ov.open_handoffs), detail: `${ov.claimed_handoffs} claimed`, urgent: ov.open_handoffs > 0, to: "/handoffs" },
    {
      key: "delivery",
      label: "Alerts delivered",
      value: `${d.delivered.count} of ${d.delivered.denominator}`,
      detail: `${pct(d.delivered.rate, 0)} sent or opened`,
      urgent: false,
    },
  ];
}

/** Delivery breakdown rows in display order. */
export function deliveryRows(d: S["DeliveryCounts"]): [string, CountRate][] {
  return (["sent", "opened", "queued", "failed", "expired", "suppressed"] as const).map((k) => [DELIVERY_LABELS[k] ?? k, d[k]]);
}

const BATCH_STATUS: Record<string, string> = { running: "still running", succeeded: "succeeded", failed: "failed" };

/** "Last batch run: ..." line for the overview. */
export function batchLine(run: S["BatchRunInfo"] | null): string {
  if (!run) return "Last batch run: never.";
  return `Last batch run: ${shortWhen(run.finished_at ?? run.started_at)}, ${BATCH_STATUS[run.status] ?? run.status}.`;
}

// --- Patients ----------------------------------------------------------------------

export interface PatientFilters {
  band_state: string;
  breach_7d: string;
  live_handoff: string;
}

export const NO_PATIENT_FILTERS: PatientFilters = { band_state: "", breach_7d: "", live_handoff: "" };

/** URL of one page of the patient list. */
export function patientsQuery(f: PatientFilters, cursor: string | null = null, limit = 100): string {
  return withParams("/api/dashboard/patients", { limit, cursor, band_state: f.band_state, breach_7d: f.breach_7d, live_handoff: f.live_handoff });
}

export type SortKey = "name" | "age" | "state" | "last" | "value" | "alerts" | "risk";
export const SORT_KEYS: readonly SortKey[] = ["state", "alerts", "last", "value", "risk", "name", "age"];
export const SORT_LABELS: Record<SortKey, string> = {
  state: "Most urgent reading",
  alerts: "Most alerts (7 days)",
  last: "Latest reading",
  value: "Reading value",
  risk: "Survey risk tier",
  name: "First name",
  age: "Age band",
};

const URGENCY: Record<BandState, number> = { breach_low: 0, breach_high: 1, pre_breach_low: 2, pre_breach_high: 3, normal: 4 };
const RISK_ORDER: Record<string, number> = { high: 0, moderate: 1, low: 2 };

type SortValue = string | number | null;
const PATIENT_SORTS: Record<SortKey, (r: S["PatientRow"]) => SortValue> = {
  name: (r) => (r.first_name ?? "").toLowerCase() || null,
  age: (r) => r.age_band,
  // Most urgent first when ascending: breach, pre-breach, normal, then no state.
  state: (r) => (r.last_reading ? (r.last_reading.band_state ? URGENCY[r.last_reading.band_state] : 5) : null),
  last: (r) => (r.last_reading ? -Date.parse(r.last_reading.measured_at) : null),
  value: (r) => (r.last_reading ? r.last_reading.value_mg_dl : null),
  alerts: (r) => -r.alerts_7d,
  risk: (r) => (r.risk ? (RISK_ORDER[r.risk.tier] ?? null) : null),
};

/** A sorted copy; `descending` reverses, missing values always stay last. */
export function sortPatients(rows: readonly S["PatientRow"][], key: SortKey, descending = false): S["PatientRow"][] {
  const pick = PATIENT_SORTS[key];
  return rows
    .map((r, i) => [r, pick(r), i] as const)
    .sort(([, a, i], [, b, j]) => {
      if (a === null) return b === null ? i - j : 1;
      if (b === null) return -1;
      const order = a < b ? -1 : a > b ? 1 : 0;
      return (descending ? -order : order) || i - j;
    })
    .map(([r]) => r);
}

const capital = (s: string): string => s.charAt(0).toUpperCase() + s.slice(1);

/** Visible cells of a patient row, from named fields only (no e-mail can leak). */
export function patientCells(row: S["PatientRow"]) {
  const last = row.last_reading;
  const l = row.limits;
  return {
    name: row.first_name || "Patient",
    age: row.age_band || "-",
    status: row.diabetes_status === "diabetes" ? "Yes" : row.diabetes_status === "no_diabetes" ? "No" : "Not set",
    last: last ? `${mgdl(last.value_mg_dl)} mg/dL` : "No readings",
    lastWhen: last ? `${CONTEXT_LABELS[last.context] ?? last.context}, ${shortWhen(last.measured_at)}` : "",
    limits: `${mgdl(l.fasting_high)} / ${mgdl(l.post_meal_high)} / ${mgdl(l.low)}`,
    alerts: String(row.alerts_7d) + (row.breach_7d ? " (breach)" : ""),
    risk: row.risk ? capital(row.risk.tier) : "Not scored",
    riskModel: row.risk ? row.risk.model_version : "",
    handoff: row.live_handoff ? (HANDOFF_LABELS[row.live_handoff] ?? row.live_handoff) : "-",
  };
}

export function diabetesLabel(status: S["PatientDetail"]["diabetes_status"]): string {
  return status === "diabetes" ? "Diabetes" : status === "no_diabetes" ? "No diabetes" : "Diabetes status not set";
}

// --- Alerts ------------------------------------------------------------------------

export interface AlertFilters {
  state: string;
  escalated: string;
  delivery_status: string;
  since: string;
  until: string;
}

export const ALERT_FILTER_KEYS = ["state", "escalated", "delivery_status", "since", "until"] as const;

/** URL of one page of the alert list; dates are local days, both included. */
export function alertsQuery(f: AlertFilters, cursor: string | null = null, limit = 25): string {
  return withParams("/api/dashboard/alerts", {
    limit,
    cursor,
    state: f.state,
    escalated: f.escalated,
    delivery_status: f.delivery_status,
    since: f.since ? dayStart(f.since) : null,
    until: f.until ? dayStart(f.until, 1) : null,
  });
}

/** Visible cells of an alert row (named fields only). */
export function alertCells(a: S["AdvisoryRow"]) {
  return {
    when: shortWhen(a.measured_at),
    name: a.first_name || "Patient",
    limit: LIMIT_LABELS[a.limit_kind] ?? a.limit_kind,
    state: bandLabel(a.state),
    tone: BAND_TONE[a.state] as Tone,
    reading: `${mgdl(a.reading_value_mg_dl)} vs ${mgdl(a.threshold_mg_dl)}`,
    escalated: a.escalated ? "Yes" : "No",
    delivery: DELIVERY_LABELS[a.delivery_status] ?? a.delivery_status,
  };
}

// --- Why is this the limit? --------------------------------------------------------

export interface WhyRow {
  kind: LimitKind;
  label: string;
  base: number;
  demo: number;
  risk: number;
  trend: number;
  sum: number;
  min: number;
  max: number;
  value: number;
  clamped: boolean;
  citation: string;
  reason: string;
  since: string;
}

/** The latest step of each limit kind, as the rows of the "Why" table. */
export function whyLimits(steps: readonly S["ThresholdStep"][]): WhyRow[] {
  const latest = new Map<LimitKind, S["ThresholdStep"]>();
  for (const s of steps) {
    const prev = latest.get(s.limit_kind);
    if (!prev || Date.parse(s.effective_from) >= Date.parse(prev.effective_from)) latest.set(s.limit_kind, s);
  }
  return CHART_LIMITS.flatMap(([kind]) => {
    const s = latest.get(kind);
    if (!s) return [];
    return [
      {
        kind,
        label: LIMIT_LABELS[kind] ?? kind,
        base: s.base_mg_dl,
        demo: s.demo_adj_mg_dl,
        risk: s.risk_adj_mg_dl,
        trend: s.trend_adj_mg_dl,
        sum: s.unclamped_mg_dl,
        min: s.hard_min_mg_dl,
        max: s.hard_max_mg_dl,
        value: s.value_mg_dl,
        clamped: s.value_mg_dl !== s.unclamped_mg_dl,
        citation: s.source_citation,
        reason: REASON_LABELS[s.reason] ?? s.reason,
        since: s.effective_from,
      },
    ];
  });
}

// --- Handoffs ----------------------------------------------------------------------

export const HANDOFF_STATUS: Record<S["HandoffSummary"]["status"], string> = {
  open: "Waiting to be claimed",
  assigned: "Claimed",
  resolved: "Resolved",
};

/** First name of a handoff's patient (the API sends the full name; only the first is shown). */
export function handoffName(h: S["HandoffSummary"]): string {
  return firstName(h.patient_name, `Patient ${h.patient_id.slice(0, 8)}`);
}

/** Waiting first, then claimed, then resolved; newest activity first inside each. */
export function sortHandoffs(list: readonly S["HandoffSummary"][]): S["HandoffSummary"][] {
  const rank = { open: 0, assigned: 1, resolved: 2 } as const;
  const at = (h: S["HandoffSummary"]) => Date.parse(h.last_message_at ?? h.opened_at);
  return [...list].sort((a, b) => rank[a.status] - rank[b.status] || at(b) - at(a));
}

/** The claim line under a thread's title. */
export function claimLine(h: S["HandoffSummary"], myName: string | null): string {
  const mine = Boolean(h.practitioner_name) && h.practitioner_name === myName;
  if (h.status === "open") return "Not claimed yet: the Advisor is still answering. Claim it (or reply) to take over the chat.";
  if (h.status === "assigned") {
    const who = mine ? "you" : firstName(h.practitioner_name, "a practitioner");
    return `Claimed by ${who}: the patient's messages come to ${mine ? "you" : "them"}.`;
  }
  return "Resolved.";
}

// --- Review ------------------------------------------------------------------------

export const VERDICTS: Record<S["ReviewIn"]["verdict"], string> = {
  correct: "Correct",
  incorrect: "Incorrect",
  unsafe: "Unsafe",
  needs_kb_update: "Needs KB update",
};
export type Verdict = keyof typeof VERDICTS;

// Mirrors NOTE_REQUIRED in app/db/audit_repository.py (the API answers 422 without one).
const NOTE_REQUIRED: readonly Verdict[] = ["incorrect", "unsafe", "needs_kb_update"];

/** True when a review with this verdict must explain itself in a note. */
export function needsNote(verdict: Verdict): boolean {
  return NOTE_REQUIRED.includes(verdict);
}

export const REVIEW_FILTERS = [
  ["unreviewed", "Unreviewed"],
  ["escalated", "Escalated"],
  ["low_confidence", "Low confidence"],
  ["unanswered", '"I don\'t know"'],
  ["all", "All"],
] as const;
export type ReviewFilter = (typeof REVIEW_FILTERS)[number][0];

/** URL of one page of the review list, optionally for one patient. */
export function reviewQuery(filter: ReviewFilter, cursor: string | null = null, limit = 25, patientId: string | null = null): string {
  return withParams("/api/practitioner/turns", { filter, limit, cursor, patient_id: patientId });
}

/** One entry of a patient's Conversation tab: an advisor answer or a handoff. */
export type TimelineItem =
  | { kind: "answer"; at: string; turn: S["ReviewableTurn"] }
  | { kind: "handoff"; at: string; handoff: S["HandoffSummary"] };

/**
 * A patient's advisor answers and handoffs together, newest first. A handoff sits
 * at its latest activity (last message, else when it opened).
 */
export function conversationTimeline(turns: readonly S["ReviewableTurn"][], handoffs: readonly S["HandoffSummary"][]): TimelineItem[] {
  const items: TimelineItem[] = [
    ...turns.map((turn): TimelineItem => ({ kind: "answer", at: turn.created_at, turn })),
    ...handoffs.map((handoff): TimelineItem => ({ kind: "handoff", at: handoff.last_message_at ?? handoff.opened_at, handoff })),
  ];
  return items.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
}

/** Tags on a review card, current verdict last. */
export function reviewTags(turn: S["ReviewableTurn"]): { text: string; urgent: boolean }[] {
  const tags: { text: string; urgent: boolean }[] = [];
  if (turn.escalated) tags.push({ text: "Escalated", urgent: true });
  if (turn.unanswered) tags.push({ text: "Didn't know", urgent: false });
  if (turn.low_confidence && turn.retrieval_confidence !== null) {
    tags.push({ text: `Low confidence ${turn.retrieval_confidence.toFixed(2)}`, urgent: false });
  }
  if (turn.spoken) tags.push({ text: "Voice", urgent: false });
  const r = turn.current_review;
  tags.push(r ? { text: VERDICTS[r.verdict] ?? r.verdict, urgent: r.verdict !== "correct" } : { text: "Unreviewed", urgent: false });
  return tags;
}

/** One source entry line of a review card. */
export function sourceLine(s: S["KbSource"]): string {
  const version = s.kb_version ? `, KB v${s.kb_version}` : "";
  const citation = s.source_citation ? `: ${s.source_citation}` : "";
  return `${s.title} (${s.entry_key}${version})${citation}`;
}

// --- Knowledge base ----------------------------------------------------------------

/** Why a draft cannot be approved yet, or null. Mirrors approve_draft (422 otherwise). */
export function kbApproveProblem(d: { body: string; source_citation?: string | null }): string | null {
  const missing: string[] = [];
  if (!d.body.trim()) missing.push("the entry text");
  if (!(d.source_citation ?? "").trim()) missing.push("a source citation");
  return missing.length ? `Add ${missing.join(" and ")} before approving.` : null;
}

/** Where a draft came from. */
export function kbDraftOrigin(e: S["KbEntryView"]): string {
  if (e.supersedes_id) {
    const version = e.supersedes_version ? ` v${e.supersedes_version}` : "";
    return `Edit of approved entry ${e.entry_key}${version}${e.created_by_name ? ` by ${firstName(e.created_by_name, "a practitioner")}` : ""}`;
  }
  if (e.origin === "proposed_from_review") return "Proposed by the weekly knowledge-base review";
  return `New entry${e.created_by_name ? ` by ${firstName(e.created_by_name, "a practitioner")}` : ""}`;
}

// Mirrors PRIORITY_LABEL in app/services/kb_upgrade.py.
export const KB_PRIORITY_LABEL = "Marked unsafe by a reviewer";

/** Tags on a draft card, the unsafe-answer priority first. */
export function kbDraftTags(e: S["KbEntryView"]): { text: string; urgent: boolean }[] {
  const tags: { text: string; urgent: boolean }[] = [];
  if (e.priority) tags.push({ text: KB_PRIORITY_LABEL, urgent: true });
  if (e.supersedes_id) tags.push({ text: "Edit", urgent: false });
  else if (e.origin === "proposed_from_review") tags.push({ text: "Proposed", urgent: false });
  return tags;
}

/** Drafts in display order: priority first, otherwise as the API sent them. */
export function kbSortDrafts(drafts: readonly S["KbEntryView"][]): S["KbEntryView"][] {
  return drafts
    .map((d, i) => [d, i] as const)
    .sort(([a, i], [b, j]) => Number(b.priority) - Number(a.priority) || i - j)
    .map(([d]) => d);
}

/** How much of the served KB a practitioner has actually approved. */
export function kbStatusLine(kb: S["KbStatusCounts"]): string {
  const parts: string[] = [];
  if (!kb.approved) parts.push("No approved entries yet.");
  else if (kb.approved_by_seed_account.count) {
    parts.push(`${kb.approved_by_seed_account.count} of ${kb.approved} approved entries were approved by the seed account, not yet reviewed by a practitioner.`);
  } else parts.push(`All ${kb.approved} approved entries were approved by a practitioner other than the seed account.`);
  const unsafe = kb.priority_drafts.count ? ` (${kb.priority_drafts.count} from answers marked unsafe)` : "";
  parts.push(`${kb.drafts} draft${kb.drafts === 1 ? "" : "s"} awaiting approval${unsafe}.`);
  return parts.join(" ");
}

// --- Audit and model card ----------------------------------------------------------

/** Summary rows in display order. */
export function auditRows(c: S["AnswerCounts"]): [string, CountRate][] {
  const v = c.verdicts;
  return [
    ["Reviewed", c.reviewed],
    ["Unreviewed", c.unreviewed],
    ["Correct (of reviewed)", v.correct],
    ["Incorrect (of reviewed)", v.incorrect],
    ["Unsafe (of reviewed)", v.unsafe],
    ["Needs KB update (of reviewed)", v.needs_kb_update],
    ["Escalated to a practitioner", c.escalated],
    ['"I don\'t know" (unanswered)', c.unanswered],
    ["Blocked by the dosing/diagnosis guard", c.blocked_by_guard],
  ];
}

export const AUDIT_KB_COLUMNS: readonly (readonly [string, (c: S["AnswerCounts"]) => CountRate])[] = [
  ["Reviewed", (c) => c.reviewed],
  ["Correct", (c) => c.verdicts.correct],
  ["Incorrect", (c) => c.verdicts.incorrect],
  ["Unsafe", (c) => c.verdicts.unsafe],
  ["Needs KB update", (c) => c.verdicts.needs_kb_update],
  ["Escalated", (c) => c.escalated],
  ["I don't know", (c) => c.unanswered],
  ["Guard", (c) => c.blocked_by_guard],
];

/** URL of the audit summary; days are local and both included. */
export function auditQuery(sinceDay: string, untilDay: string): string {
  return withParams("/api/practitioner/audit/summary", {
    since: sinceDay ? dayStart(sinceDay) : null,
    until: untilDay ? dayStart(untilDay, 1) : null,
  });
}

// Precision, recall and F1 come before ROC-AUC, so ROC-AUC is never read on its own.
// Mirrors CardMetrics in app/ml/model_card.py.
export const MODEL_METRICS: readonly (readonly [keyof S["CardMetrics"], string])[] = [
  ["precision", "Precision (flagged people who have diabetes)"],
  ["recall", "Recall (people with diabetes who are flagged)"],
  ["f1", "F1"],
  ["roc_auc", "ROC-AUC"],
  ["pr_auc", "PR-AUC (average precision)"],
  ["brier", "Brier score (lower is better)"],
];

const RETRAIN_DECISIONS: Record<string, string> = {
  promoted: "new model promoted",
  kept: "current model kept",
  dry_run: "dry run, not promoted",
};

/** "Last retrain: ..." from the newest run (or null), without the date. */
export function retrainLine(run: S["RetrainRunView"] | null): string {
  if (!run) return "Last retrain: none yet. Retraining is manual (python -m app.jobs.retrain).";
  if (run.status === "running") return "Last retrain: still running.";
  const decision = run.decision ? (RETRAIN_DECISIONS[run.decision] ?? run.decision) : "no decision";
  const failed = run.status === "failed" ? `, training failed${run.error ? ` (${run.error})` : ""}` : "";
  const fresh = run.report?.new;
  const metrics = fresh ? `; candidate ${fresh.version}: precision ${fresh.precision.toFixed(3)}, recall ${fresh.recall.toFixed(3)}` : "";
  return `Last retrain: ${decision}${failed}${metrics}.`;
}

/** The "x % of flagged people do not have diabetes" sentence. */
export function flaggedLine(f: CountRate): string {
  return `Of the ${f.denominator.toLocaleString()} people the model flagged, ${f.count.toLocaleString()} (${pct(f.rate)}) do not have diabetes: FP / (FP + TP).`;
}
