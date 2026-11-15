// Patient app (/app): Supabase email login, Web Push alerts (R3), and the help chat
// (R4) with typed messages, hold-to-record voice and "Talk to a doctor". A live
// banner appears when an advisory arrives over Supabase Realtime; practitioner
// replies arrive on the private chat:<auth uid> topic.
"use strict";

const { $, UUID, say, guarded, clock } = window.DA;
const STATE_TITLES = {
  pre_breach_high: "Glucose close to your upper limit",
  breach_high: "High glucose reading",
  pre_breach_low: "Glucose close to your lower limit",
  breach_low: "Low glucose reading",
};
// Recording limits: the API takes about a minute (CHAT_AUDIO_MAX_BYTES); shorter than
// half a second is almost always an accidental tap.
const MAX_RECORDING_MS = 60_000;
const MIN_RECORDING_MS = 500;
// Fallback refresh while a practitioner conversation is live, in case Realtime drops.
const POLL_MS = 20_000;

let channels = [];
let pollTimer = null;
let handoffLive = false;
// Spoken replies keyed by bubble id: {url, text} (url null -> browser speech).
const spoken = new Map();
const recorder = { media: null, stream: null, chunks: [], started: 0, timer: null, tick: null, cancelled: false };

/** Show the banner for an advisory announced over Realtime. */
function showAdvisoryBanner(payload) {
  if (!payload || !UUID.test(payload.advisory_id || "")) return;
  $("new-advisory-title").textContent = STATE_TITLES[payload.state] || "New glucose advice";
  $("new-advisory-link").href = `/app/advisory/${payload.advisory_id}`;
  $("new-advisory").hidden = false;
}

// --- Rendering -----------------------------------------------------------------------

const WHO = { assistant: "Advisor", practitioner: "Practitioner" };

/** One chat bubble. Text is set with textContent (never HTML). */
function bubble(message) {
  const el = document.createElement("div");
  el.className = `bubble ${message.role}`;
  el.dataset.id = message.id;
  if (WHO[message.role]) {
    const who = document.createElement("span");
    who.className = "who";
    who.textContent = WHO[message.role];
    el.append(who);
  }
  el.append(document.createTextNode(message.text));
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = `${message.spoken ? "\u{1F3A4} " : ""}${clock(message.created_at)}`;
  el.append(meta);
  const audio = spoken.get(message.id);
  if (audio) {
    const play = document.createElement("button");
    play.type = "button";
    play.className = "play";
    play.textContent = "▶ Play";
    play.setAttribute("aria-label", "Play the spoken reply");
    play.addEventListener("click", () => playReply(audio));
    el.append(play);
  }
  return el;
}

function renderHandoff(handoff) {
  handoffLive = Boolean(handoff);
  const el = $("handoff");
  if (!handoff) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  if (!handoff.has_practitioner) {
    el.textContent = "Your request is saved for the care team. Please also contact your doctor directly.";
  } else if (handoff.status === "assigned") {
    el.textContent = "You are talking to a practitioner. Your messages go to them.";
  } else {
    el.textContent = "A practitioner has been notified. Your messages go to them.";
  }
}

function renderMessages(messages) {
  const box = $("messages");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  box.replaceChildren(...messages.map(bubble));
  if (!messages.length) {
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent = "Send a reading like “fasting 132” or “190 after lunch”, or ask a question.";
    box.append(hint);
  }
  if (nearBottom || messages.length) box.scrollTop = box.scrollHeight;
}

async function loadConversation() {
  const data = await window.DA.api("/api/chat/messages");
  renderMessages(data.messages);
  renderHandoff(data.handoff);
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  if (handoffLive) {
    pollTimer = setTimeout(() => {
      if (document.visibilityState === "visible") loadConversation().catch(() => {});
      else schedulePoll();
    }, POLL_MS);
  }
}

// --- Spoken replies ----------------------------------------------------------------

function speakWithBrowser(text) {
  if (!("speechSynthesis" in window)) {
    say("This browser cannot play voice replies. Please read the text.", true);
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-IN";
  utterance.rate = 0.95;
  window.speechSynthesis.speak(utterance);
}

/** Play a spoken reply; fall back to the browser voice if the mp3 fails. */
async function playReply(audio) {
  const text = audio.text.split("\n\nSource:")[0];
  if (!audio.url) {
    speakWithBrowser(text);
    return;
  }
  const player = $("player");
  player.src = audio.url;
  try {
    await player.play();
  } catch {
    audio.url = null; // expired signed URL or blocked: use the browser voice
    speakWithBrowser(text);
  }
}

// --- Sending -------------------------------------------------------------------------

function setBusy(busy) {
  $("send").disabled = busy;
  $("doctor").disabled = busy;
  $("text").disabled = busy;
}

function showPending(text) {
  const box = $("messages");
  const mine = document.createElement("div");
  mine.className = "bubble patient";
  mine.textContent = text;
  const typing = document.createElement("div");
  typing.className = "typing";
  typing.textContent = "Advisor is replying…";
  box.append(mine, typing);
  box.scrollTop = box.scrollHeight;
}

/** POST /api/chat with a FormData body, then refresh the conversation. */
async function send(form, pendingText) {
  form.append("message_id", window.DA.messageId());
  setBusy(true);
  showPending(pendingText);
  try {
    const reply = await window.DA.api("/api/chat", "POST", form);
    if (reply.transcript !== null && reply.transcript !== undefined) {
      const audio = { url: reply.audio_url, text: reply.reply };
      spoken.set(`${reply.turn_id}:a`, audio);
      // The patient just interacted, so most browsers allow playing straight away.
      playReply(audio).catch(() => {});
    }
  } finally {
    setBusy(false);
    await loadConversation();
  }
}

async function sendText() {
  const text = $("text").value.trim();
  if (!text) return;
  const form = new FormData();
  form.append("text", text);
  $("text").value = "";
  await send(form, text);
}

async function talkToDoctor() {
  const form = new FormData();
  form.append("request_practitioner", "true");
  form.append("text", "Talk to a doctor");
  await send(form, "Talk to a doctor");
}

// --- Hold-to-record -------------------------------------------------------------------

function pickMimeType() {
  const options = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
  return options.find((t) => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || "";
}

async function startRecording() {
  if (recorder.media || $("send").disabled) return;
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    say("Voice messages are not supported in this browser. Please type instead.", true);
    return;
  }
  recorder.cancelled = false;
  try {
    recorder.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    say("Microphone access was refused. Allow it in the browser's site settings.", true);
    return;
  }
  const mimeType = pickMimeType();
  recorder.media = new MediaRecorder(recorder.stream, mimeType ? { mimeType } : undefined);
  recorder.chunks = [];
  recorder.media.ondataavailable = (e) => { if (e.data && e.data.size) recorder.chunks.push(e.data); };
  recorder.media.onstop = () => { finishRecording().catch((err) => say(err.message || String(err), true)); };
  recorder.media.start();
  recorder.started = Date.now();
  $("record").classList.add("recording");
  $("record").setAttribute("aria-pressed", "true");
  recorder.tick = setInterval(() => {
    const s = Math.floor((Date.now() - recorder.started) / 1000);
    $("rec-hint").textContent = `Recording 0:${String(s).padStart(2, "0")} — release to send`;
  }, 250);
  recorder.timer = setTimeout(stopRecording, MAX_RECORDING_MS);
}

function stopRecording(cancel = false) {
  if (!recorder.media || recorder.media.state === "inactive") return;
  recorder.cancelled = cancel;
  recorder.media.stop();
}

async function finishRecording() {
  clearTimeout(recorder.timer);
  clearInterval(recorder.tick);
  const elapsed = Date.now() - recorder.started;
  const type = (recorder.media && recorder.media.mimeType) || "audio/webm";
  recorder.stream.getTracks().forEach((t) => t.stop());
  recorder.media = null;
  $("record").classList.remove("recording");
  $("record").setAttribute("aria-pressed", "false");
  $("rec-hint").textContent = "Hold the microphone to talk (up to a minute).";
  if (recorder.cancelled || elapsed < MIN_RECORDING_MS || !recorder.chunks.length) {
    if (!recorder.cancelled) say("Hold the microphone button while you speak, then let go.");
    return;
  }
  const blob = new Blob(recorder.chunks, { type });
  const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
  const form = new FormData();
  form.append("audio", blob, `voice.${ext}`);
  say("");
  await send(form, "\u{1F3A4} Voice message…");
}

function wireRecordButton() {
  const btn = $("record");
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    btn.setPointerCapture(e.pointerId);
    startRecording().catch((err) => say(err.message || String(err), true));
  });
  btn.addEventListener("pointerup", () => stopRecording());
  btn.addEventListener("pointercancel", () => stopRecording(true));
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
  // Keyboard: hold Space or Enter.
  btn.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) {
      e.preventDefault();
      startRecording().catch((err) => say(err.message || String(err), true));
    }
  });
  btn.addEventListener("keyup", (e) => {
    if (e.key === " " || e.key === "Enter") stopRecording();
  });
}

// --- Session ---------------------------------------------------------------------------

async function leaveChannels() {
  for (const ch of channels) await window.DA.sb.removeChannel(ch);
  channels = [];
}

/** Show the signed-in or signed-out panels; practitioners go to their own page. */
async function render() {
  const session = await window.DA.session();
  const user = session ? session.user : null;
  $("signed-out").hidden = Boolean(user);
  await leaveChannels();
  if (!user) {
    $("signed-in").hidden = true;
    $("chat").hidden = true;
    return;
  }
  $("who").textContent = user.email;
  $("signed-in").hidden = false;
  let me = null;
  try {
    me = await window.DA.api("/api/me");
  } catch (err) {
    $("chat").hidden = true;
    say(err.message || String(err), true);
    return;
  }
  if (me.role === "practitioner") {
    location.replace("/app/practitioner");
    return;
  }
  $("chat").hidden = false;
  const uid = session.user.id;
  channels = [
    await window.DA.listen(`advisories:${uid}`, "advisory", showAdvisoryBanner),
    await window.DA.listen(`chat:${uid}`, "message", () => { loadConversation().catch(() => {}); }),
  ].filter(Boolean);
  await loadConversation();
}

async function init() {
  // Old notifications linked to /app/?advisory=<id>; send them to the advisory page.
  const params = new URLSearchParams(location.search);
  const advisoryId = params.get("advisory");
  if (advisoryId && UUID.test(advisoryId)) {
    location.replace(`/app/advisory/${advisoryId}`);
    return;
  }
  const { sb } = await window.DA.init();
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
  $("enable").addEventListener("click", guarded(async () => {
    await window.DA.enableAlerts();
    say("Alerts are on for this device.");
  }));
  $("disable").addEventListener("click", guarded(async () => {
    say(await window.DA.disableAlerts() ? "Alerts are off for this device." : "Alerts were already off on this device.");
  }));
  $("composer").addEventListener("submit", guarded(sendText));
  $("text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("doctor").addEventListener("click", guarded(talkToDoctor));
  wireRecordButton();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !$("chat").hidden) loadConversation().catch(() => {});
  });
  await render();
  // From the advisory page's "Talk to a doctor": /app/?connect=1&advisory_id=<id>.
  if (params.get("connect") === "1" && !$("chat").hidden) {
    history.replaceState(null, "", "/app/");
    const form = new FormData();
    form.append("request_practitioner", "true");
    form.append("text", "Talk to a doctor");
    const fromAdvisory = params.get("advisory_id");
    if (fromAdvisory && UUID.test(fromAdvisory)) form.append("advisory_id", fromAdvisory);
    await send(form, "Talk to a doctor").catch((err) => say(err.message || String(err), true));
  }
}

init().catch((err) => say(err.message || String(err), true));
