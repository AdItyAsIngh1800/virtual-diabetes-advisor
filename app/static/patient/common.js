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

  return {
    $, UUID, say, init, session, api, enableAlerts, disableAlerts, listen, guarded, clock, messageId,
    dayLabel, withDays, renderWithDays, VERDICTS, needsNote, reviewQuery, reviewTags,
    kbApproveProblem, kbDraftOrigin, kbDraftTags, kbSortDrafts, KB_PRIORITY_LABEL,
    get sb() { return sb; },
    get config() { return config; },
  };
})();
