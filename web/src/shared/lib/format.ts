/**
 * Formatting helpers shared by both apps: times, day separators, mg/dL values and
 * rates. Ported from the classic common.js with the same behaviour (its tests are
 * mirrored in format.test.ts). Times are the viewer's local time; the demo cohort
 * and the tests run in Asia/Kolkata.
 */

/** "12:04" style local time. */
export function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Local calendar day of a Date as "YYYY-MM-DD" (for comparing days). */
export function dayKey(date: Date): string {
  const two = (n: number): string => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${two(date.getMonth() + 1)}-${two(date.getDate())}`;
}

/**
 * Label for a message's day: "Today", "Yesterday", "16 Nov", or "16 Nov 2025" in
 * another year. Local time, so a turn from late last night is not "Today".
 */
export function dayLabel(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (dayKey(date) === dayKey(now)) return "Today";
  const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
  if (dayKey(date) === dayKey(yesterday)) return "Yesterday";
  const opts: Intl.DateTimeFormatOptions = { day: "numeric", month: "short" };
  if (date.getFullYear() !== now.getFullYear()) opts.year = "numeric";
  return date.toLocaleDateString("en-GB", opts);
}

/** A day separator in a message list. */
export interface DaySeparator {
  day: string;
  key: string;
}

/** Messages with a day separator before the first message of each local day. */
export function withDays<T extends { created_at: string }>(
  messages: readonly T[],
  now: Date = new Date(),
): (T | DaySeparator)[] {
  const out: (T | DaySeparator)[] = [];
  let last: string | null = null;
  for (const m of messages) {
    const key = dayKey(new Date(m.created_at));
    if (key !== last) {
      out.push({ day: dayLabel(m.created_at, now), key: `day-${key}` });
      last = key;
    }
    out.push(m);
  }
  return out;
}

/** True for the separators that withDays inserts. */
export function isDay<T extends object>(item: T | DaySeparator): item is DaySeparator {
  return "day" in item;
}

/** mg/dL with one decimal only when needed: 130 -> "130", 123.5 -> "123.5". */
export function mgdl(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "-";
  const n = typeof value === "string" ? Number(value) : value;
  if (!Number.isFinite(n)) return "-";
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}

/** "+5", "-3.5" or "0" for an adjustment. */
export function signed(v: number): string {
  if (v > 0) return `+${mgdl(v)}`;
  if (v < 0) return `-${mgdl(-v)}`;
  return "0";
}

/** Short date + time in the viewer's zone, e.g. "17 Nov, 14:05". */
export function shortWhen(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Medium date and short time, e.g. "17 Nov 2026, 14:05". */
export function when(iso: string): string {
  return new Date(iso).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

/** A rate as a percentage, or "n/a" when its denominator is 0 (the API sends null). */
export function pct(rate: number | null | undefined, digits = 1): string {
  return rate === null || rate === undefined ? "n/a" : `${(rate * 100).toFixed(digits)}%`;
}

/** A count with what it is out of. */
export interface CountRate {
  count: number;
  denominator: number;
  rate: number | null;
}

/** "12 of 40 (30.0%)": a count always shown with what it is out of. */
export function countOf(cr: CountRate): string {
  return `${cr.count} of ${cr.denominator} (${pct(cr.rate)})`;
}

/** "YYYY-MM-DD" of a timestamp in the viewer's timezone (for date inputs). */
export function localDay(iso: string, minusMs = 0): string {
  return dayKey(new Date(new Date(iso).getTime() - minusMs));
}

/** Local "YYYY-MM-DD" day -> ISO instant of its local midnight (plus `addDays`). */
export function dayStart(day: string, addDays = 0): string {
  const d = new Date(`${day}T00:00:00`);
  d.setDate(d.getDate() + addDays);
  return d.toISOString();
}

/** URL with only the parameters that are set (null, undefined and "" are left out). */
export function withParams(path: string, params: Record<string, string | number | boolean | null | undefined>): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `${path}?${parts.join("&")}` : path;
}

/** First word of a name: practitioners and patients are shown by first name only. */
export function firstName(name: string | null | undefined, fallback: string): string {
  const first = (name ?? "").trim().split(/\s+/)[0];
  return first || fallback;
}

/** Random client message id (de-duplicates retried sends). */
export function messageId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
