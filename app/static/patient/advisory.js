// Advisory page (R3): reading, personal limit, advice and the voice advisory.
// Loads GET /api/advisories/{id} with the Supabase session; the API checks ownership
// and records that the advisory was opened.
"use strict";

const $ = (id) => document.getElementById(id);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const HEADLINES = {
  pre_breach_high: "Your glucose is close to your upper limit",
  breach_high: "Your glucose is high",
  pre_breach_low: "Your glucose is close to your lower limit",
  breach_low: "Your glucose is low",
};
const BADGES = {
  pre_breach_high: "Running high",
  breach_high: "High",
  pre_breach_low: "Running low",
  breach_low: "Low",
};
const CONTEXTS = { fasting: "Fasting reading", post_meal: "After-meal reading", random: "Reading" };
// speechSynthesis voice languages per advisory language (skill: en-IN / hi-IN / ta-IN).
const VOICE_LANGS = { en: "en-IN", hi: "hi-IN", ta: "ta-IN" };

let advisory = null;
let speaking = false;

/** Show a status line; errors are red. */
function say(text, isError = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", isError);
}

/** "126.0" -> "126", "48.5" -> "48.5". */
function mgdl(value) {
  return String(Number(value));
}

function setPlayLabel(text) {
  $("play-label").textContent = text;
}

/** Fill the page from the API response. */
function render(a) {
  const breach = a.state.startsWith("breach_");
  $("advisory").className = breach || a.urgent ? "tone-bad" : "tone-warn";
  $("badge").textContent = a.urgent ? "Very low" : BADGES[a.state];
  $("headline").textContent = a.urgent ? "Your glucose is very low" : HEADLINES[a.state];
  $("reading-label").textContent = CONTEXTS[a.context] || "Reading";
  $("reading").textContent = mgdl(a.reading_value_mg_dl);
  $("limit-label").textContent = a.direction === "high" ? "Your upper limit" : "Your lower limit";
  $("limit").textContent = mgdl(a.threshold_mg_dl);
  $("measured").textContent = `Measured ${new Date(a.measured_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
  $("message").textContent = a.message_text;
  $("urgent").hidden = !a.urgent;
  $("loading").hidden = true;
  $("advisory").hidden = false;
  if (a.audio_url) {
    $("audio").src = a.audio_url;
  }
}

/** Speak the advice with the browser's own voice (no recording, or it failed). */
function speakWithBrowser() {
  if (!("speechSynthesis" in window)) {
    say("This browser cannot play the voice advice. Please read the text above.", true);
    return;
  }
  if (speaking) {
    window.speechSynthesis.cancel();
    return;
  }
  const utterance = new SpeechSynthesisUtterance(advisory.spoken_text);
  utterance.lang = VOICE_LANGS[advisory.language] || "en-IN";
  utterance.rate = 0.95;
  utterance.onstart = () => { speaking = true; setPlayLabel("Stop"); };
  utterance.onend = utterance.onerror = () => { speaking = false; setPlayLabel("Play voice advice"); };
  window.speechSynthesis.speak(utterance);
}

/** Play the recorded mp3; fall back to speechSynthesis if it is missing or fails. */
async function play() {
  say("");
  const audio = $("audio");
  if (!advisory.audio_url) {
    speakWithBrowser();
    return;
  }
  if (!audio.paused) {
    audio.pause();
    audio.currentTime = 0;
    return;
  }
  try {
    await audio.play();
  } catch {
    // Expired signed URL, network error or unsupported codec: use the browser voice.
    advisory.audio_url = null;
    speakWithBrowser();
  }
}

/** Load the advisory for the id in /app/advisory/{id}. */
async function load(sb) {
  const id = location.pathname.split("/").filter(Boolean).pop();
  if (!UUID.test(id || "")) {
    throw new Error("This link is not a valid advisory link.");
  }
  const { data } = await sb.auth.getSession();
  if (!data.session) {
    $("loading").innerHTML = 'Please <a href="/app/">sign in</a> on this device first, then tap the alert again.';
    return;
  }
  const res = await fetch(`/api/advisories/${id}`, {
    headers: { Authorization: `Bearer ${data.session.access_token}` },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(res.status === 403 ? `You cannot open this advisory: ${detail}.` : detail);
  }
  advisory = await res.json();
  render(advisory);
}

async function init() {
  const audio = $("audio");
  audio.addEventListener("play", () => setPlayLabel("Stop"));
  audio.addEventListener("pause", () => setPlayLabel("Play voice advice"));
  audio.addEventListener("ended", () => setPlayLabel("Play voice advice"));
  audio.addEventListener("error", () => {
    if (advisory && advisory.audio_url && audio.src) {
      advisory.audio_url = null;
    }
  });
  $("play").addEventListener("click", () => { play().catch((err) => say(err.message || String(err), true)); });
  // R4: practitioner connect. The chat page sends the request (with this advisory's id,
  // which the API only accepts if it is the signed-in patient's own advisory).
  $("doctor").addEventListener("click", () => {
    const id = location.pathname.split("/").filter(Boolean).pop();
    const query = UUID.test(id || "") ? `&advisory_id=${id}` : "";
    location.assign(`/app/?connect=1${query}`);
  });

  const res = await fetch("/api/app-config");
  if (!res.ok) {
    throw new Error("Server is missing SUPABASE_ANON_KEY; see the setup steps.");
  }
  const config = await res.json();
  const sb = window.supabase.createClient(config.supabase_url, config.supabase_anon_key);
  await load(sb);
}

init().catch((err) => {
  $("loading").hidden = true;
  say(err.message || String(err), true);
});
