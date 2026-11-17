/**
 * Pure chat and recording logic for the patient app (tested in chat.test.ts). The UI
 * never rewrites advisor text: these helpers only decide how a message is framed
 * (an emergency card, a source footer), and every character is still shown.
 */

// The fixed emergency and urgent replies (app/services/chat.py) tell the patient to
// call 112; those replies are framed as a red alert card with a tap-to-call button.
const CALL_112 = /\bcall 112\b/i;

/** True when an advisor reply is one of the emergency/urgent templates. */
export function isEmergencyReply(role: string, text: string): boolean {
  return role === "assistant" && CALL_112.test(text);
}

/**
 * Split a knowledge-base answer into its answer and its "Source: ..." footer (which
 * carries the not-yet-reviewed notice). Both parts are shown; nothing is dropped.
 */
export function splitSource(text: string): { body: string; source: string | null } {
  const at = text.indexOf("\n\nSource:");
  if (at < 0) return { body: text, source: null };
  return { body: text.slice(0, at), source: text.slice(at + 2) };
}

/** What the browser voice reads for a reply: the answer, not the source footer. */
export function spokenText(text: string): string {
  return splitSource(text).body;
}

// --- Hold-to-record -----------------------------------------------------------------

// The API takes about a minute (CHAT_AUDIO_MAX_BYTES, 60 s); shorter than half a
// second is almost always an accidental tap.
export const MAX_RECORDING_MS = 60_000;
export const MIN_RECORDING_MS = 500;
// Sliding this far (px) away from the button while holding cancels on release.
export const CANCEL_DISTANCE_PX = 80;

export type RecordingState =
  | { phase: "idle" }
  | { phase: "starting" }
  | { phase: "recording"; startedAt: number; cancelArmed: boolean }
  | { phase: "sending" };

export type RecordingEvent =
  | { type: "press" }
  | { type: "started"; at: number }
  | { type: "move"; dx: number; dy: number }
  | { type: "release"; at: number }
  | { type: "cancel" }
  | { type: "failed" }
  | { type: "sent" };

/** What to do with a finished recording. */
export type RecordingOutcome = "send" | "too-short" | "cancelled";

/** Decide the outcome of a release: send, too short, or cancelled. */
export function releaseOutcome(state: RecordingState, at: number): RecordingOutcome {
  if (state.phase !== "recording" || state.cancelArmed) return "cancelled";
  return at - state.startedAt < MIN_RECORDING_MS ? "too-short" : "send";
}

/** The recording state machine behind the hold-to-talk button. */
export function recordingReducer(state: RecordingState, event: RecordingEvent): RecordingState {
  switch (event.type) {
    case "press":
      return state.phase === "idle" ? { phase: "starting" } : state;
    case "started":
      return state.phase === "starting" ? { phase: "recording", startedAt: event.at, cancelArmed: false } : state;
    case "move":
      if (state.phase !== "recording") return state;
      return { ...state, cancelArmed: Math.hypot(event.dx, event.dy) >= CANCEL_DISTANCE_PX };
    case "release":
      if (state.phase !== "recording") return state.phase === "starting" ? { phase: "idle" } : state;
      return releaseOutcome(state, event.at) === "send" ? { phase: "sending" } : { phase: "idle" };
    case "cancel":
    case "failed":
    case "sent":
      return { phase: "idle" };
  }
}

/** "0:07" for elapsed milliseconds. */
export function recordingClock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Best supported recording type (Safari records mp4). */
export function pickMimeType(isSupported: (t: string) => boolean): string {
  const options = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];
  return options.find((t) => isSupported(t)) ?? "";
}

/** Upload filename extension for a recording type. */
export function audioExtension(type: string): string {
  if (type.includes("mp4")) return "m4a";
  if (type.includes("ogg")) return "ogg";
  return "webm";
}
