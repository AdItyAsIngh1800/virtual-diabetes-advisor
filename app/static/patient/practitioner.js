// Practitioner page (/app/practitioner, R4): open handoffs of assigned patients, the
// conversation, a reply box, a resolve button and "Enable alerts" (Web Push for new
// handoffs). New handoffs and patient messages arrive on the private Realtime topic
// handoffs:<auth uid>; the API re-checks the assignment on every call.
"use strict";

const { $, say, guarded, clock } = window.DA;
const POLL_MS = 20_000;
const TRIGGERS = { red_flag: "Red flag", practitioner_request: "Asked for help", breach_escalation: "Breach" };
const WHO = { patient: "Patient", assistant: "Advisor (automatic)", practitioner: "You" };

let selected = null;
let channel = null;
let pollTimer = null;

function bubble(message) {
  const el = document.createElement("div");
  el.className = `bubble ${message.role}`;
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = WHO[message.role] || message.role;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = `${message.spoken ? "voice · " : ""}${clock(message.created_at)}`;
  el.append(who, document.createTextNode(message.text), meta);
  return el;
}

function label(h) {
  return h.patient_name || `Patient ${h.patient_id.slice(0, 8)}`;
}

function renderList(handoffs) {
  const list = $("list");
  list.replaceChildren();
  $("empty").hidden = handoffs.some((h) => h.status !== "resolved");
  for (const h of handoffs) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("aria-current", String(h.id === selected));
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = label(h);
    const tags = document.createElement("div");
    const trig = document.createElement("span");
    trig.className = `tag${h.trigger === "red_flag" ? " urgent" : ""}`;
    trig.textContent = TRIGGERS[h.trigger] || h.trigger;
    const st = document.createElement("span");
    st.className = "tag";
    st.textContent = h.status;
    const when = document.createElement("span");
    when.className = "muted";
    when.textContent = ` ${clock(h.last_message_at || h.opened_at)}`;
    tags.append(trig, st, when);
    btn.append(name, tags);
    btn.addEventListener("click", () => { select(h.id).catch((err) => say(err.message, true)); });
    li.append(btn);
    list.append(li);
  }
}

async function loadList() {
  const handoffs = await window.DA.api("/api/practitioner/handoffs");
  renderList(handoffs);
  if (!selected) {
    const first = handoffs.find((h) => h.status !== "resolved");
    if (first) await select(first.id);
  }
}

async function select(id) {
  selected = id;
  const detail = await window.DA.api(`/api/practitioner/handoffs/${id}`);
  const h = detail.handoff;
  $("thread").hidden = false;
  $("thread-title").textContent = label(h);
  $("thread-meta").textContent = `${TRIGGERS[h.trigger] || h.trigger} · ${h.status} · opened ${new Date(h.opened_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}${h.reason ? ` · ${h.reason}` : ""}`;
  const box = $("messages");
  box.replaceChildren(...detail.messages.map(bubble));
  box.scrollTop = box.scrollHeight;
  const resolved = h.status === "resolved";
  $("reply-form").hidden = resolved;
  $("resolved-note").hidden = !resolved;
  await loadListOnly();
}

async function loadListOnly() {
  renderList(await window.DA.api("/api/practitioner/handoffs"));
}

async function refresh() {
  await loadList();
  if (selected) await select(selected);
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (document.visibilityState === "visible") await refresh().catch(() => {});
    schedulePoll();
  }, POLL_MS);
}

async function sendReply() {
  const text = $("reply").value.trim();
  if (!text || !selected) return;
  $("send").disabled = true;
  try {
    await window.DA.api(`/api/practitioner/handoffs/${selected}/messages`, "POST", { text });
    $("reply").value = "";
    await select(selected);
  } finally {
    $("send").disabled = false;
  }
}

async function resolveSelected() {
  if (!selected) return;
  const note = window.prompt("Resolution note (optional):", "");
  if (note === null) return;
  await window.DA.api(`/api/practitioner/handoffs/${selected}/resolve`, "POST", { note: note || null });
  say("Handoff resolved.");
  await select(selected);
}

async function render() {
  const session = await window.DA.session();
  if (channel) {
    await window.DA.sb.removeChannel(channel);
    channel = null;
  }
  $("signed-out").hidden = Boolean(session);
  $("signed-in").hidden = !session;
  $("inbox").hidden = true;
  if (!session) return;
  $("who").textContent = session.user.email;
  const me = await window.DA.api("/api/me");
  if (me.role !== "practitioner") {
    location.replace("/app/");
    return;
  }
  $("inbox").hidden = false;
  channel = await window.DA.listen(`handoffs:${session.user.id}`, "handoff", () => { refresh().catch(() => {}); });
  await loadList();
  schedulePoll();
}

async function init() {
  const { sb } = await window.DA.init();
  sb.auth.onAuthStateChange(() => { setTimeout(() => { render().catch((err) => say(err.message, true)); }, 0); });
  $("login-form").addEventListener("submit", guarded(async () => {
    const { error } = await sb.auth.signInWithPassword({ email: $("email").value, password: $("password").value });
    if (error) throw error;
  }));
  $("signout").addEventListener("click", guarded(async () => { await sb.auth.signOut(); }));
  $("enable").addEventListener("click", guarded(async () => {
    await window.DA.enableAlerts();
    say("Alerts are on: you will be notified about new handoffs on this device.");
  }));
  $("refresh").addEventListener("click", guarded(refresh));
  $("reply-form").addEventListener("submit", guarded(sendReply));
  $("resolve").addEventListener("click", guarded(resolveSelected));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !$("inbox").hidden) refresh().catch(() => {});
  });
  await render();
}

init().catch((err) => say(err.message || String(err), true));
