// Shared helpers for the patient app (/app) and the practitioner page (/app/practitioner):
// Supabase client bootstrap, authenticated /api calls, Web Push subscribe/unsubscribe.
// Talks only to this origin's /api routes and to Supabase Auth / Realtime.
"use strict";

window.DA = (() => {
  const $ = (id) => document.getElementById(id);
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let sb = null;
  let config = null;

  /** Show a status line; errors are red. */
  function say(text, isError = false) {
    const el = $("status");
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("error", isError);
  }

  /** Decode a base64url VAPID key into the bytes pushManager.subscribe expects. */
  function base64UrlToBytes(value) {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  }

  /** True when two key buffers hold the same bytes. */
  function sameKey(a, b) {
    if (!a || a.byteLength !== b.byteLength) return false;
    const x = new Uint8Array(a);
    return x.every((v, i) => v === b[i]);
  }

  /** Load /api/app-config and create the Supabase client (once). */
  async function init() {
    if (sb) return { sb, config };
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" })
        .catch((err) => say(`Service worker failed: ${err.message}`, true));
    }
    const res = await fetch("/api/app-config");
    // 503 carries the reason: missing SUPABASE_ANON_KEY, or
    // "database unreachable (port 5432 blocked?)".
    if (!res.ok) throw await failure(res);
    config = await res.json();
    sb = window.supabase.createClient(config.supabase_url, config.supabase_anon_key);
    return { sb, config };
  }

  /** Current session, or null when signed out. */
  async function session() {
    const { data } = await sb.auth.getSession();
    return data.session;
  }

  /** Turn a failed response into an Error carrying the API's detail. */
  async function failure(res) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.status = res.status;
    return err;
  }

  /**
   * Call one of our /api routes with the session token. `body` may be a plain object
   * (sent as JSON) or FormData (multipart, for voice uploads).
   */
  async function api(path, method = "GET", body = undefined) {
    const s = await session();
    const headers = s ? { Authorization: `Bearer ${s.access_token}` } : {};
    let payload;
    if (body instanceof FormData) {
      payload = body;
    } else if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      payload = JSON.stringify(body);
    }
    const res = await fetch(path, { method, headers, body: payload });
    if (!res.ok) throw await failure(res);
    return res.status === 204 ? null : res.json();
  }

  /** Ask for permission, subscribe this browser and register it with the API. */
  async function enableAlerts() {
    if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
      throw new Error("This browser cannot receive push alerts here. On iPhone, add the app to the Home Screen first.");
    }
    if (!config.vapid_public_key) {
      throw new Error("The server has no VAPID key yet (run scripts/gen_vapid_keys.py).");
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      throw new Error("Notifications are blocked. Allow them in the browser's site settings.");
    }
    const registration = await navigator.serviceWorker.ready;
    const key = base64UrlToBytes(config.vapid_public_key);
    let sub = await registration.pushManager.getSubscription();
    // A subscription made with an older VAPID key cannot be reused: replace it.
    if (sub && !sameKey(sub.options.applicationServerKey, key)) {
      await sub.unsubscribe();
      sub = null;
    }
    sub = sub || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    await api("/api/push/subscribe", "POST", sub.toJSON());
  }

  /** Revoke this browser's subscription on the server and in the browser. Returns false if none. */
  async function disableAlerts() {
    const registration = await navigator.serviceWorker.ready;
    const sub = await registration.pushManager.getSubscription();
    if (!sub) return false;
    await api("/api/push/subscribe", "DELETE", { endpoint: sub.endpoint });
    await sub.unsubscribe();
    return true;
  }

  /** Join a private Realtime topic (RLS: only topics named after your own auth uid). */
  async function listen(topic, event, handler) {
    const s = await session();
    if (!s) return null;
    await sb.realtime.setAuth(s.access_token);
    return sb.channel(topic, { config: { private: true } })
      .on("broadcast", { event }, ({ payload }) => handler(payload))
      .subscribe();
  }

  /** Run an async handler and show its error, if any. */
  function guarded(handler) {
    return async (event) => {
      if (event && event.preventDefault) event.preventDefault();
      try {
        say("");
        await handler(event);
      } catch (err) {
        say(err.message || String(err), true);
      }
    };
  }

  /** "12:04" style local time. */
  function clock(iso) {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  /** Local calendar day of a Date, as "YYYY-MM-DD" (for comparing days). */
  function dayKey(date) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  }

  /**
   * Label for a message's day: "Today", "Yesterday", "16 Nov", or "16 Nov 2025" in
   * another year. Local time, so a turn from late last night is not "Today".
   */
  function dayLabel(iso, now = new Date()) {
    const date = new Date(iso);
    if (dayKey(date) === dayKey(now)) return "Today";
    const yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);
    if (dayKey(date) === dayKey(yesterday)) return "Yesterday";
    const opts = { day: "numeric", month: "short" };
    if (date.getFullYear() !== now.getFullYear()) opts.year = "numeric";
    return date.toLocaleDateString("en-GB", opts);
  }

  /**
   * Messages as [{day}, message, message, {day}, ...]: a day separator before the
   * first message of each local calendar day.
   */
  function withDays(messages, now = new Date()) {
    const out = [];
    let last = null;
    for (const m of messages) {
      const key = dayKey(new Date(m.created_at));
      if (key !== last) {
        out.push({ day: dayLabel(m.created_at, now) });
        last = key;
      }
      out.push(m);
    }
    return out;
  }

  /** Render messages with day separators; renderBubble(message) returns an element. */
  function renderWithDays(messages, renderBubble) {
    return withDays(messages).map((item) => {
      if (!("day" in item)) return renderBubble(item);
      const sep = document.createElement("div");
      sep.className = "day";
      sep.setAttribute("role", "separator");
      sep.textContent = item.day;
      return sep;
    });
  }

  /** Random client message id (de-duplicates retried sends). */
  function messageId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  // --- Answer review (R5, practitioner page) ---------------------------------------
  const VERDICTS = Object.freeze({
    correct: "Correct", incorrect: "Incorrect", unsafe: "Unsafe", needs_kb_update: "Needs KB update",
  });
  // Mirrors NOTE_REQUIRED in app/db/audit_repository.py (the API answers 422 without one).
  const NOTE_REQUIRED = Object.freeze(["incorrect", "unsafe", "needs_kb_update"]);

  /** True when a review with this verdict must explain itself in a note. */
  function needsNote(verdict) {
    return NOTE_REQUIRED.includes(verdict);
  }

  /** URL of one page of the review list (cursor: next_cursor of the previous page). */
  function reviewQuery(filter, cursor = null, limit = 25) {
    let url = `/api/practitioner/turns?filter=${encodeURIComponent(filter)}&limit=${limit}`;
    if (cursor) url += `&cursor=${encodeURIComponent(cursor)}`;
    return url;
  }

  /** Tags shown on a review card: [{ text, urgent }], current verdict last. */
  function reviewTags(turn) {
    const tags = [];
    if (turn.escalated) tags.push({ text: "Escalated", urgent: true });
    if (turn.unanswered) tags.push({ text: "Didn't know", urgent: false });
    if (turn.low_confidence && turn.retrieval_confidence !== null) {
      tags.push({ text: `Low confidence ${turn.retrieval_confidence.toFixed(2)}`, urgent: false });
    }
    if (turn.spoken) tags.push({ text: "Voice", urgent: false });
    const review = turn.current_review;
    tags.push(review
      ? { text: VERDICTS[review.verdict] || review.verdict, urgent: review.verdict !== "correct" }
      : { text: "Unreviewed", urgent: false });
    return tags;
  }

  // --- Knowledge base (R5, practitioner page) --------------------------------------

  /**
   * Why a draft cannot be approved yet, or null when it can. Mirrors approve_draft in
   * app/db/kb_repository.py: the API answers 422 without a body or a source citation.
   */
  function kbApproveProblem(draft) {
    const missing = [];
    if (!(draft.body || "").trim()) missing.push("the entry text");
    if (!(draft.source_citation || "").trim()) missing.push("a source citation");
    return missing.length ? `Add ${missing.join(" and ")} before approving.` : null;
  }

  /** One line saying where a draft came from. */
  function kbDraftOrigin(entry) {
    if (entry.supersedes_id) {
      const version = entry.supersedes_version ? ` v${entry.supersedes_version}` : "";
      return `Edit of approved entry ${entry.entry_key}${version}${entry.created_by_name ? ` by ${entry.created_by_name}` : ""}`;
    }
    if (entry.origin === "proposed_from_review") return "Proposed by the weekly knowledge-base review";
    return `New entry${entry.created_by_name ? ` by ${entry.created_by_name}` : ""}`;
  }

  // Mirrors PRIORITY_LABEL in app/services/kb_upgrade.py.
  const KB_PRIORITY_LABEL = "Marked unsafe by a reviewer";

  /** Tags on a draft card: [{ text, urgent }], the unsafe-answer priority first. */
  function kbDraftTags(entry) {
    const tags = [];
    if (entry.priority) tags.push({ text: KB_PRIORITY_LABEL, urgent: true });
    if (entry.supersedes_id) tags.push({ text: "Edit", urgent: false });
    else if (entry.origin === "proposed_from_review") tags.push({ text: "Proposed", urgent: false });
    return tags;
  }

  /** Drafts in display order: priority first, otherwise as the API sent them. */
  function kbSortDrafts(drafts) {
    return drafts.map((d, i) => [d, i])
      .sort(([a, i], [b, j]) => (Number(Boolean(b.priority)) - Number(Boolean(a.priority))) || i - j)
      .map(([d]) => d);
  }

  // --- Audit tab (R5, practitioner page) -------------------------------------------
  // The API never sends a rate without its count and denominator ({count, denominator,
  // rate}); the page never shows one without them either.

  /** A rate as a percentage, or "n/a" when its denominator is 0 (the API sends null). */
  function pct(rate, digits = 1) {
    return rate === null || rate === undefined ? "n/a" : `${(rate * 100).toFixed(digits)}%`;
  }

  /** "12 of 40 (30.0%)": a count always shown with what it is out of. */
  function countOf(cr) {
    return `${cr.count} of ${cr.denominator} (${pct(cr.rate)})`;
  }

  /** Summary table rows: [label, {count, denominator, rate}], in display order. */
  function auditRows(counts) {
    const v = counts.verdicts;
    return [
      ["Reviewed", counts.reviewed],
      ["Unreviewed", counts.unreviewed],
      ["Correct (of reviewed)", v.correct],
      ["Incorrect (of reviewed)", v.incorrect],
      ["Unsafe (of reviewed)", v.unsafe],
      ["Needs KB update (of reviewed)", v.needs_kb_update],
      ["Escalated to a practitioner", counts.escalated],
      ['"I don\'t know" (unanswered)', counts.unanswered],
      ["Blocked by the dosing/diagnosis guard", counts.blocked_by_guard],
    ];
  }

  /** Per-KB-version table columns: [header, pick(counts) -> {count, denominator, rate}]. */
  const AUDIT_KB_COLUMNS = Object.freeze([
    ["Reviewed", (c) => c.reviewed],
    ["Correct", (c) => c.verdicts.correct],
    ["Incorrect", (c) => c.verdicts.incorrect],
    ["Unsafe", (c) => c.verdicts.unsafe],
    ["Needs KB update", (c) => c.verdicts.needs_kb_update],
    ["Escalated", (c) => c.escalated],
    ["I don't know", (c) => c.unanswered],
    ["Guard", (c) => c.blocked_by_guard],
  ]);

  /**
   * URL of the summary for a date range. Dates are "YYYY-MM-DD" in the viewer's own
   * timezone and both days are included (until is sent as the next local midnight).
   */
  function auditQuery(sinceDay, untilDay) {
    const params = [];
    if (sinceDay) params.push(`since=${encodeURIComponent(new Date(`${sinceDay}T00:00:00`).toISOString())}`);
    if (untilDay) {
      const end = new Date(`${untilDay}T00:00:00`);
      end.setDate(end.getDate() + 1);
      params.push(`until=${encodeURIComponent(end.toISOString())}`);
    }
    return `/api/practitioner/audit/summary${params.length ? `?${params.join("&")}` : ""}`;
  }

  /** "YYYY-MM-DD" of a timestamp in the viewer's timezone (for the date inputs). */
  function localDay(iso, minusMs = 0) {
    const d = new Date(new Date(iso).getTime() - minusMs);
    const two = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${two(d.getMonth() + 1)}-${two(d.getDate())}`;
  }

  /** The KB status line: how much of the served KB a practitioner has actually approved. */
  function kbStatusLine(kb) {
    const parts = [];
    if (!kb.approved) {
      parts.push("No approved entries yet.");
    } else if (kb.approved_by_seed_account.count) {
      parts.push(`${kb.approved_by_seed_account.count} of ${kb.approved} approved entries were approved by the seed account, not yet reviewed by a practitioner.`);
    } else {
      parts.push(`All ${kb.approved} approved entries were approved by a practitioner other than the seed account.`);
    }
    const unsafe = kb.priority_drafts.count ? ` (${kb.priority_drafts.count} from answers marked unsafe)` : "";
    parts.push(`${kb.drafts} draft${kb.drafts === 1 ? "" : "s"} awaiting approval${unsafe}.`);
    return parts.join(" ");
  }

  // Model card metrics in display order: precision, recall and F1 before ROC-AUC, so
  // ROC-AUC is never read on its own. Mirrors CardMetrics in app/ml/model_card.py.
  const MODEL_METRICS = Object.freeze([
    ["precision", "Precision (flagged people who have diabetes)"],
    ["recall", "Recall (people with diabetes who are flagged)"],
    ["f1", "F1"],
    ["roc_auc", "ROC-AUC"],
    ["pr_auc", "PR-AUC (average precision)"],
    ["brier", "Brier score (lower is better)"],
  ]);

  const RETRAIN_DECISIONS = Object.freeze({
    promoted: "new model promoted", kept: "current model kept", dry_run: "dry run, not promoted",
  });

  /** "Last retrain: ..." from the newest run (or null), without the date. */
  function retrainLine(run) {
    if (!run) return "Last retrain: none yet. Retraining is manual (python -m app.jobs.retrain).";
    if (run.status === "running") return "Last retrain: still running.";
    const decision = RETRAIN_DECISIONS[run.decision] || run.decision;
    const failed = run.status === "failed" ? `, training failed${run.error ? ` (${run.error})` : ""}` : "";
    const fresh = run.report && run.report.new;
    const metrics = fresh
      ? `; candidate ${fresh.version}: precision ${fresh.precision.toFixed(3)}, recall ${fresh.recall.toFixed(3)}`
      : "";
    return `Last retrain: ${decision}${failed}${metrics}.`;
  }

  // --- Dashboard (R6, practitioner page) -------------------------------------------
  // Pure helpers: URLs, labels, sorting, the band maths and the chart's coordinate
  // maths. practitioner.js only turns their output into DOM/SVG. Patients are shown by
  // first name; no helper here reads an e-mail, phone number or push endpoint.

  // Shown next to every risk tier (R6 safety rule): the survey score moves no limit.
  const RISK_LABEL = "Survey-based diabetes risk (information only, does not change limits)";

  // Text (with a direction glyph) for each band state: colour is never the only signal.
  const BAND_LABELS = Object.freeze({
    normal: "In range",
    pre_breach_high: "▲ Near high limit",
    pre_breach_low: "▼ Near low limit",
    breach_high: "▲ Above limit",
    breach_low: "▼ Below limit",
  });
  const BAND_TONE = Object.freeze({
    normal: "", pre_breach_high: "warn", pre_breach_low: "warn", breach_high: "urgent", breach_low: "urgent",
  });
  const LIMIT_LABELS = Object.freeze({ fasting_high: "Fasting", post_meal_high: "Post-meal", low: "Low" });
  const CONTEXT_LABELS = Object.freeze({ fasting: "Fasting", post_meal: "Post-meal", random: "Random" });
  const DELIVERY_LABELS = Object.freeze({
    queued: "Queued", sent: "Sent", opened: "Opened", failed: "Failed", expired: "Expired", suppressed: "Suppressed",
  });
  const REASON_LABELS = Object.freeze({
    initial: "First computed", batch: "Batch recompute", trend_revision: "Trend revision", profile_change: "Profile changed",
  });
  const HANDOFF_LABELS = Object.freeze({ open: "Waiting", assigned: "Claimed" });

  /** Label for a band state; "No limit yet" when the API sends null. */
  function bandLabel(state) {
    return state ? BAND_LABELS[state] || state : "No limit yet";
  }

  /** URL with only the parameters that are set (null, undefined and "" are left out). */
  function withParams(path, params = {}) {
    const parts = Object.entries(params)
      .filter(([, v]) => v !== null && v !== undefined && v !== "")
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    return parts.length ? `${path}?${parts.join("&")}` : path;
  }

  /** Local "YYYY-MM-DD" day -> ISO instant of its local midnight (plus `addDays`). */
  function dayStart(day, addDays = 0) {
    const d = new Date(`${day}T00:00:00`);
    d.setDate(d.getDate() + addDays);
    return d.toISOString();
  }

  /** URL of one page of the alert list; dates are local days, both included. */
  function alertsQuery(filters = {}, cursor = null, limit = 25) {
    return withParams("/api/dashboard/alerts", {
      limit,
      cursor,
      state: filters.state,
      escalated: filters.escalated,
      delivery_status: filters.delivery_status,
      since: filters.since ? dayStart(filters.since) : null,
      until: filters.until ? dayStart(filters.until, 1) : null,
    });
  }

  /** URL of one page of the patient list. */
  function patientsQuery(filters = {}, cursor = null, limit = 100) {
    return withParams("/api/dashboard/patients", {
      limit, cursor, band_state: filters.band_state, breach_7d: filters.breach_7d, live_handoff: filters.live_handoff,
    });
  }

  // Sort keys of the patient table. Missing values always sort last.
  const PATIENT_SORTS = Object.freeze({
    name: (r) => (r.first_name || "").toLowerCase() || null,
    age: (r) => r.age_band,
    // Most urgent first when ascending: breach, then pre-breach, then normal, then none.
    state: (r) => (r.last_reading ? { breach_low: 0, breach_high: 1, pre_breach_low: 2, pre_breach_high: 3, normal: 4 }[r.last_reading.band_state] ?? 5 : null),
    last: (r) => (r.last_reading ? -Date.parse(r.last_reading.measured_at) : null),
    value: (r) => (r.last_reading ? r.last_reading.value_mg_dl : null),
    alerts: (r) => -r.alerts_7d,
    risk: (r) => (r.risk ? { high: 0, moderate: 1, low: 2 }[r.risk.tier] : null),
  });

  /** A sorted copy of patient rows; `descending` reverses, missing values stay last. */
  function sortPatients(rows, key, descending = false) {
    const pick = PATIENT_SORTS[key] || PATIENT_SORTS.name;
    return rows.map((r, i) => [r, pick(r), i]).sort(([, a, i], [, b, j]) => {
      if (a === null || a === undefined) return b === null || b === undefined ? i - j : 1;
      if (b === null || b === undefined) return -1;
      const order = a < b ? -1 : a > b ? 1 : 0;
      return (descending ? -order : order) || i - j;
    }).map(([r]) => r);
  }

  /** Short date + time in the viewer's zone, e.g. "17 Nov, 14:05". */
  function shortWhen(iso) {
    return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  /** mg/dL with one decimal only when needed: 130 -> "130", 123.5 -> "123.5". */
  function mgdl(value) {
    if (value === null || value === undefined) return "-";
    return Number.isInteger(value) ? String(value) : value.toFixed(1);
  }

  /**
   * Visible cells of one patient row, in column order. Built from named fields only,
   * so an unexpected field (an e-mail, a phone number) can never reach the page.
   */
  function patientCells(row) {
    const last = row.last_reading;
    const l = row.limits;
    return {
      name: row.first_name || "Patient",
      age: row.age_band || "-",
      status: row.diabetes_status === "diabetes" ? "Yes" : row.diabetes_status === "no_diabetes" ? "No" : "Not set",
      last: last ? `${mgdl(last.value_mg_dl)} mg/dL` : "No readings",
      lastWhen: last ? `${CONTEXT_LABELS[last.context] || last.context}, ${shortWhen(last.measured_at)}` : "",
      state: last ? bandLabel(last.band_state) : "-",
      limits: `${mgdl(l.fasting_high)} / ${mgdl(l.post_meal_high)} / ${mgdl(l.low)}`,
      alerts: String(row.alerts_7d) + (row.breach_7d ? " (breach)" : ""),
      risk: row.risk ? `${row.risk.tier[0].toUpperCase()}${row.risk.tier.slice(1)}` : "Not scored",
      riskModel: row.risk ? row.risk.model_version : "",
      handoff: row.live_handoff ? HANDOFF_LABELS[row.live_handoff] || row.live_handoff : "-",
    };
  }

  /** Visible cells of one alert row (named fields only, as patientCells). */
  function alertCells(a) {
    return {
      when: shortWhen(a.measured_at),
      name: a.first_name || "Patient",
      limit: LIMIT_LABELS[a.limit_kind] || a.limit_kind,
      state: bandLabel(a.state),
      reading: `${mgdl(a.reading_value_mg_dl)} vs ${mgdl(a.threshold_mg_dl)}`,
      escalated: a.escalated ? "Yes" : "No",
      delivery: DELIVERY_LABELS[a.delivery_status] || a.delivery_status,
    };
  }

  /** KPI tiles of the overview: [{ label, value, detail }]. Rates always say "x of y". */
  function overviewTiles(ov) {
    const d = ov.delivery;
    return [
      { key: "patients", label: "Active patients", value: String(ov.active_patients), detail: "" },
      { key: "readings", label: "Readings today", value: String(ov.readings), detail: "" },
      { key: "pre", label: "Pre-breach alerts", value: String(ov.pre_breach_alerts), detail: "last 24 h" },
      { key: "breach", label: "Breaches", value: String(ov.breaches), detail: "last 24 h", urgent: ov.breaches > 0 },
      { key: "hypo", label: "Below 54 mg/dL", value: String(ov.below_54_readings), detail: "last 24 h", urgent: ov.below_54_readings > 0 },
      { key: "open", label: "Handoffs waiting", value: String(ov.open_handoffs), detail: `${ov.claimed_handoffs} claimed` },
      {
        key: "delivery",
        label: "Alerts delivered",
        value: pct(d.delivered.rate, 0),
        detail: `${d.delivered.count} of ${d.delivered.denominator} sent or opened`,
      },
    ];
  }

  /** Advisory delivery breakdown rows: [label, {count, denominator, rate}]. */
  function deliveryRows(d) {
    return ["sent", "opened", "queued", "failed", "expired", "suppressed"].map((k) => [DELIVERY_LABELS[k], d[k]]);
  }

  /** "Last batch run: ..." line for the overview. */
  function batchLine(run) {
    if (!run) return "Last batch run: never.";
    const status = { running: "still running", succeeded: "succeeded", failed: "failed" }[run.status] || run.status;
    return `Last batch run: ${shortWhen(run.finished_at || run.started_at)}, ${status}.`;
  }

  // --- Band maths: mirrors classify_reading in app/services/thresholds.py ----------
  // Python uses exact decimals; doubles are not exact (130 * 0.95 = 123.49999...), so
  // values (one decimal place) and the band (up to four) are compared as integers.
  const TENTHS = 10;
  const BAND_UNITS = 10000;

  function asTenths(value) { return Math.round(value * TENTHS); }

  /** [lower, upper] mg/dL of the pre-breach band of a limit (band 0.05 = 5 %). */
  function bandEdges(limit, direction, band) {
    return direction === "upper" ? [limit * (1 - band), limit] : [limit, limit * (1 + band)];
  }

  /** Band state of a reading; limits = {fasting_high, post_meal_high, low}. null if a limit is missing. */
  function bandState(value, context, limits, band) {
    const upperKind = { fasting: "fasting_high", post_meal: "post_meal_high", random: null }[context];
    const low = limits.low;
    const upper = upperKind ? limits[upperKind] : null;
    if (low === null || low === undefined || (upperKind && (upper === null || upper === undefined))) return null;
    const b = Math.round(band * BAND_UNITS);
    const x = asTenths(value) * BAND_UNITS;
    const L = asTenths(low);
    if (x <= L * BAND_UNITS) return "breach_low";
    if (upperKind && x >= asTenths(upper) * BAND_UNITS) return "breach_high";
    if (x <= L * (BAND_UNITS + b)) return "pre_breach_low";
    if (upperKind && x >= asTenths(upper) * (BAND_UNITS - b)) return "pre_breach_high";
    return "normal";
  }

  // --- Chart maths (inline SVG, no library) ----------------------------------------

  /** Map v from [d0, d1] onto [r0, r1] (linear). A zero-width domain maps to the middle. */
  function scale(v, d0, d1, r0, r1) {
    if (d1 === d0) return (r0 + r1) / 2;
    return r0 + ((v - d0) / (d1 - d0)) * (r1 - r0);
  }

  /** Round tick values covering [min, max]: steps of 1, 2, 2.5 or 5 x 10^n, about `count` of them. */
  function niceTicks(min, max, count = 5) {
    if (!(max > min)) return [min];
    const raw = (max - min) / count;
    const mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
    const ticks = [];
    for (let t = Math.ceil(min / step) * step; t <= max + step / 1e6; t += step) ticks.push(Math.round(t * 1e6) / 1e6);
    return ticks;
  }

  /**
   * Step segments of one limit over [t0, t1] (ms): [{ from, to, value, beforeFirst }].
   * Before the first stored row the first value is drawn with beforeFirst = true (the
   * batch job judged that backlog against it, the chart dashes it).
   */
  function stepSegments(steps, kind, t0, t1) {
    const rows = steps.filter((s) => s.limit_kind === kind)
      .map((s) => ({ at: Date.parse(s.effective_from), value: s.value_mg_dl }))
      .sort((a, b) => a.at - b.at);
    if (!rows.length) return [];
    const out = [];
    if (rows[0].at > t0) out.push({ from: t0, to: Math.min(rows[0].at, t1), value: rows[0].value, beforeFirst: true });
    rows.forEach((r, i) => {
      const from = Math.max(r.at, t0);
      const to = i + 1 < rows.length ? Math.min(rows[i + 1].at, t1) : t1;
      if (to > from) out.push({ from, to, value: r.value, beforeFirst: false });
    });
    return out;
  }

  /** The y domain: every reading and limit (with its band) in view, padded, from round ticks. */
  function yDomain(values, limitValues, band) {
    const all = [...values, ...limitValues, ...limitValues.map((v) => v * (1 - band))];
    if (!all.length) return [40, 250];
    const lo = Math.min(...all);
    const hi = Math.max(...all);
    const pad = Math.max(10, (hi - lo) * 0.08);
    // Whole tens: the axis does not start at 0 (glucose is never near it), and the
    // ticks drawn inside come from niceTicks.
    return [Math.max(0, Math.floor((lo - pad) / 10) * 10), Math.ceil((hi + pad) / 10) * 10];
  }

  const CHART_LIMITS = Object.freeze([["fasting_high", "upper"], ["post_meal_high", "upper"], ["low", "lower"]]);

  /**
   * Everything the patient chart draws, in pixels: plot box, axis ticks, one entry per
   * limit with its step segments and band rectangles, and one point per reading.
   * `detail` is the /api/dashboard/patients/{id} body.
   */
  function chartModel(detail, width, height, margin = { top: 12, right: 12, bottom: 28, left: 44 }) {
    const t0 = Date.parse(detail.window.since);
    const t1 = Date.parse(detail.window.until);
    const band = detail.pre_breach_band;
    const plot = { x0: margin.left, x1: width - margin.right, y0: margin.top, y1: height - margin.bottom };
    const limits = CHART_LIMITS.map(([kind, direction]) => ({ kind, direction, segments: stepSegments(detail.thresholds, kind, t0, t1) }))
      .filter((l) => l.segments.length);
    const [v0, v1] = yDomain(
      detail.readings.map((r) => r.value_mg_dl),
      limits.flatMap((l) => l.segments.map((s) => s.value)),
      band,
    );
    const x = (t) => scale(t, t0, t1, plot.x0, plot.x1);
    const y = (v) => scale(v, v0, v1, plot.y1, plot.y0); // SVG y grows downwards
    const clampY = (py) => Math.min(plot.y1, Math.max(plot.y0, py));
    for (const l of limits) {
      for (const s of l.segments) {
        const [lo, hi] = bandEdges(s.value, l.direction, band);
        s.x0 = x(s.from);
        s.x1 = x(s.to);
        s.y = y(s.value);
        s.bandTop = clampY(y(hi));
        s.bandBottom = clampY(y(lo));
      }
    }
    const DAY = 86_400_000;
    const days = Math.max(1, Math.round((t1 - t0) / DAY));
    const every = Math.max(1, Math.ceil(days / Math.max(2, Math.floor((plot.x1 - plot.x0) / 70))));
    const xTicks = [];
    const first = new Date(t0);
    first.setHours(0, 0, 0, 0);
    for (let t = first.getTime() + DAY; t <= t1; t += every * DAY) xTicks.push({ t, x: x(t) });
    return {
      width, height, plot, domain: [v0, v1], band, limits,
      yTicks: niceTicks(v0, v1, 5).map((v) => ({ v, y: y(v) })),
      xTicks,
      points: detail.readings.map((r) => ({
        id: r.id, x: x(Date.parse(r.measured_at)), y: y(r.value_mg_dl), value: r.value_mg_dl, context: r.context,
        state: r.band_state, breach: r.band_state === "breach_high" || r.band_state === "breach_low",
        beforeFirst: r.before_first_limit, measured_at: r.measured_at,
      })),
      beforeFirst: limits.some((l) => l.segments.some((s) => s.beforeFirst)),
    };
  }

  /** Index of the point nearest (px, py) within `radius` px, or -1. */
  function nearestPoint(points, px, py, radius = 24) {
    let best = -1;
    let bestD = radius * radius;
    points.forEach((p, i) => {
      const d = (p.x - px) ** 2 + (p.y - py) ** 2;
      if (d <= bestD) { best = i; bestD = d; }
    });
    return best;
  }

  /**
   * "Why is this the limit?" for the current limits: the latest step of each kind as
   * { kind, label, base, demo, risk, trend, sum, min, max, value, clamped, citation, reason, since }.
   */
  function whyLimits(steps) {
    const latest = {};
    for (const s of steps) {
      const prev = latest[s.limit_kind];
      if (!prev || Date.parse(s.effective_from) >= Date.parse(prev.effective_from)) latest[s.limit_kind] = s;
    }
    return CHART_LIMITS.map(([kind]) => latest[kind]).filter(Boolean).map((s) => ({
      kind: s.limit_kind,
      label: LIMIT_LABELS[s.limit_kind],
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
      reason: REASON_LABELS[s.reason] || s.reason,
      since: s.effective_from,
    }));
  }

  /** "+5", "-3.5" or "0" for an adjustment. */
  function signed(v) {
    return v > 0 ? `+${mgdl(v)}` : v < 0 ? `-${mgdl(-v)}` : "0";
  }

  return {
    $, UUID, say, init, session, api, enableAlerts, disableAlerts, listen, guarded, clock, messageId,
    dayLabel, withDays, renderWithDays, VERDICTS, needsNote, reviewQuery, reviewTags,
    kbApproveProblem, kbDraftOrigin, kbDraftTags, kbSortDrafts, KB_PRIORITY_LABEL,
    pct, countOf, auditRows, AUDIT_KB_COLUMNS, auditQuery, localDay, kbStatusLine, MODEL_METRICS,
    RETRAIN_DECISIONS, retrainLine,
    RISK_LABEL, BAND_LABELS, BAND_TONE, LIMIT_LABELS, CONTEXT_LABELS, DELIVERY_LABELS, REASON_LABELS,
    HANDOFF_LABELS, bandLabel, withParams, alertsQuery, patientsQuery, PATIENT_SORTS, sortPatients,
    shortWhen, mgdl, patientCells, alertCells, overviewTiles, deliveryRows, batchLine, bandEdges,
    bandState, scale, niceTicks, stepSegments, yDomain, chartModel, nearestPoint, whyLimits, signed,
    get sb() { return sb; },
    get config() { return config; },
  };
})();
