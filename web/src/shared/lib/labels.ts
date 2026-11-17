/**
 * Words for every state, so colour is never the only signal (both apps). The
 * practitioner wording is ported from the classic common.js; the patient wording is
 * plainer and never mentions a risk score.
 */
import type { S } from "@/shared/api/client";

export type BandState = NonNullable<S["ReadingPointOut"]["band_state"]>;
export type LimitKind = S["ThresholdStep"]["limit_kind"];
export type Tone = "neutral" | "warn" | "urgent" | "good";

// Text with a direction glyph for each band state (practitioner app).
export const BAND_LABELS: Record<BandState, string> = {
  normal: "In range",
  pre_breach_high: "▲ Near high limit",
  pre_breach_low: "▼ Near low limit",
  breach_high: "▲ Above limit",
  breach_low: "▼ Below limit",
};

// The same states in plain words for patients.
export const PATIENT_BAND_LABELS: Record<BandState, string> = {
  normal: "In your range",
  pre_breach_high: "▲ Close to your upper limit",
  pre_breach_low: "▼ Close to your lower limit",
  breach_high: "▲ Above your limit",
  breach_low: "▼ Below your limit",
};

export const BAND_TONE: Record<BandState, Tone> = {
  normal: "good",
  pre_breach_high: "warn",
  pre_breach_low: "warn",
  breach_high: "urgent",
  breach_low: "urgent",
};

export const LIMIT_LABELS: Record<string, string> = { fasting_high: "Fasting", post_meal_high: "Post-meal", low: "Low" };
export const CONTEXT_LABELS: Record<string, string> = { fasting: "Fasting", post_meal: "Post-meal", random: "Random" };
export const PATIENT_CONTEXT_LABELS: Record<string, string> = { fasting: "Fasting", post_meal: "After a meal", random: "Any time" };
export const DELIVERY_LABELS: Record<string, string> = {
  queued: "Queued",
  sent: "Sent",
  opened: "Opened",
  failed: "Failed",
  expired: "Expired",
  suppressed: "Suppressed",
};
export const REASON_LABELS: Record<string, string> = {
  initial: "First computed",
  batch: "Batch recompute",
  trend_revision: "Trend revision",
  profile_change: "Profile changed",
};
export const HANDOFF_LABELS: Record<string, string> = { open: "Waiting", assigned: "Claimed", resolved: "Resolved" };
export const TRIGGERS: Record<string, string> = { red_flag: "Red flag", practitioner_request: "Asked for help", breach_escalation: "Breach" };

/** Label for a band state; "No limit yet" when the API sends null. */
export function bandLabel(state: BandState | null | undefined): string {
  return state ? (BAND_LABELS[state] ?? state) : "No limit yet";
}

/** Tone of a band state (neutral when unknown). */
export function bandTone(state: BandState | null | undefined): Tone {
  return state ? BAND_TONE[state] : "neutral";
}

// Shown next to every risk tier (R6 safety rule): the survey score moves no limit.
export const RISK_LABEL = "Survey-based diabetes risk (information only, does not change limits)";
