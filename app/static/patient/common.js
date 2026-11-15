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
    if (!res.ok) throw new Error("Server is missing SUPABASE_ANON_KEY; see the setup steps.");
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

  /** Random client message id (de-duplicates retried sends). */
  function messageId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  return {
    $, UUID, say, init, session, api, enableAlerts, disableAlerts, listen, guarded, clock, messageId,
    get sb() { return sb; },
    get config() { return config; },
  };
})();
