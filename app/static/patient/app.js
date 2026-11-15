// Patient test page: Supabase email login + Web Push subscription (R3), and a live
// banner when an advisory arrives over Supabase Realtime while the app is open.
// Talks only to this origin's /api routes and to Supabase Auth / Realtime.
"use strict";

const $ = (id) => document.getElementById(id);
let config = null;
let sb = null;
let advisoryChannel = null;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATE_TITLES = {
  pre_breach_high: "Glucose close to your upper limit",
  breach_high: "High glucose reading",
  pre_breach_low: "Glucose close to your lower limit",
  breach_low: "Low glucose reading",
};

/** Show a status line; errors are red. */
function say(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
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

/** Current Supabase access token, or null when signed out. */
async function accessToken() {
  const { data } = await sb.auth.getSession();
  return data.session ? data.session.access_token : null;
}

/** Call one of our /api routes with the session token; throws with the API's detail. */
async function api(path, method, body) {
  const token = await accessToken();
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

/** Show the banner for an advisory announced over Realtime. */
function showAdvisoryBanner(payload) {
  if (!payload || !UUID.test(payload.advisory_id || "")) return;
  $("new-advisory-title").textContent = STATE_TITLES[payload.state] || "New glucose advice";
  $("new-advisory-link").href = `/app/advisory/${payload.advisory_id}`;
  $("new-advisory").hidden = false;
}

/**
 * Listen on this login's private topic advisories:<auth uid>. The 0007 RLS policy on
 * realtime.messages only lets a patient join their own topic.
 */
async function listenForAdvisories(session) {
  if (advisoryChannel) {
    await sb.removeChannel(advisoryChannel);
    advisoryChannel = null;
  }
  if (!session) return;
  await sb.realtime.setAuth(session.access_token);
  advisoryChannel = sb
    .channel(`advisories:${session.user.id}`, { config: { private: true } })
    .on("broadcast", { event: "advisory" }, ({ payload }) => showAdvisoryBanner(payload))
    .subscribe();
}

/** Show the signed-in or signed-out panel and (re)join the advisory channel. */
async function render() {
  const { data } = await sb.auth.getSession();
  const user = data.session ? data.session.user : null;
  $("signed-out").hidden = Boolean(user);
  $("signed-in").hidden = !user;
  if (user) $("who").textContent = user.email;
  await listenForAdvisories(data.session);
}

/** Ask for permission, subscribe this browser and register it with the API. */
async function enableAlerts() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    say("This browser cannot receive push alerts here. On iPhone, add the app to the Home Screen first.", true);
    return;
  }
  if (!config.vapid_public_key) {
    say("The server has no VAPID key yet (run scripts/gen_vapid_keys.py).", true);
    return;
  }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    say("Notifications are blocked. Allow them in the browser's site settings.", true);
    return;
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
  say("Alerts are on for this device.");
}

/** Revoke this browser's subscription on the server and in the browser. */
async function disableAlerts() {
  const registration = await navigator.serviceWorker.ready;
  const sub = await registration.pushManager.getSubscription();
  if (!sub) {
    say("Alerts were already off on this device.");
    return;
  }
  await api("/api/push/subscribe", "DELETE", { endpoint: sub.endpoint });
  await sub.unsubscribe();
  say("Alerts are off for this device.");
}

/** Run an async handler and show its error, if any. */
function guarded(handler) {
  return async (event) => {
    if (event) event.preventDefault();
    try {
      say("");
      await handler();
    } catch (err) {
      say(err.message || String(err), true);
    }
  };
}

async function init() {
  // Old notifications linked to /app/?advisory=<id>; send them to the advisory page.
  const advisoryId = new URLSearchParams(location.search).get("advisory");
  if (advisoryId && UUID.test(advisoryId)) {
    location.replace(`/app/advisory/${advisoryId}`);
    return;
  }
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/app/sw.js", { scope: "/app/" }).catch((err) => say(`Service worker failed: ${err.message}`, true));
  }
  const res = await fetch("/api/app-config");
  if (!res.ok) {
    say("Server is missing SUPABASE_ANON_KEY; see the setup steps.", true);
    return;
  }
  config = await res.json();
  sb = window.supabase.createClient(config.supabase_url, config.supabase_anon_key);
  // Deferred: Supabase warns against awaiting its API inside this callback.
  sb.auth.onAuthStateChange(() => { setTimeout(() => { render().catch((err) => say(err.message, true)); }, 0); });

  $("login-form").addEventListener("submit", guarded(async () => {
    const { error } = await sb.auth.signInWithPassword({ email: $("email").value, password: $("password").value });
    if (error) throw error;
  }));
  $("signup").addEventListener("click", guarded(async () => {
    const { data, error } = await sb.auth.signUp({
      email: $("email").value,
      password: $("password").value,
      options: { emailRedirectTo: `${location.origin}/app/` },
    });
    if (error) throw error;
    say(data.session ? "Account created." : "Check your email to confirm the account, then sign in.");
  }));
  $("signout").addEventListener("click", guarded(async () => { await sb.auth.signOut(); }));
  $("enable").addEventListener("click", guarded(enableAlerts));
  $("disable").addEventListener("click", guarded(disableAlerts));
  await render();
}

init().catch((err) => say(err.message || String(err), true));
