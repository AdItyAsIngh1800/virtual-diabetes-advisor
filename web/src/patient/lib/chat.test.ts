import { describe, expect, it } from "vitest";

import {
  audioExtension,
  isEmergencyReply,
  pickMimeType,
  recordingClock,
  recordingReducer,
  releaseOutcome,
  splitSource,
  spokenText,
  type RecordingState,
} from "@/patient/lib/chat";

describe("emergency framing", () => {
  it("marks the fixed 112 replies from the advisor only", () => {
    expect(isEmergencyReply("assistant", "Call 112 now or go to the nearest hospital.")).toBe(true);
    expect(isEmergencyReply("assistant", "Eat a snack. Do not drive. Call 112 if you feel worse.")).toBe(true);
    expect(isEmergencyReply("patient", "should I call 112?")).toBe(false);
    expect(isEmergencyReply("assistant", "Your fasting reading 132 is in range.")).toBe(false);
  });
});

describe("source footer", () => {
  const text = "Walk after meals.\n\nSource: Exercise (ADA 2026)\nStudent prototype: not yet reviewed by a practitioner.";

  it("keeps every character across body and footer", () => {
    const { body, source } = splitSource(text);
    expect(body).toBe("Walk after meals.");
    expect(source).toBe("Source: Exercise (ADA 2026)\nStudent prototype: not yet reviewed by a practitioner.");
    expect(`${body}\n\n${source}`).toBe(text);
  });

  it("leaves text without a source alone", () => {
    expect(splitSource("Hello")).toEqual({ body: "Hello", source: null });
  });

  it("speaks the answer, not the footer", () => {
    expect(spokenText(text)).toBe("Walk after meals.");
  });
});

describe("recording state", () => {
  const idle: RecordingState = { phase: "idle" };

  it("goes press -> started -> release (long enough) -> sending -> sent", () => {
    let s = recordingReducer(idle, { type: "press" });
    expect(s.phase).toBe("starting");
    s = recordingReducer(s, { type: "started", at: 1000 });
    expect(s).toEqual({ phase: "recording", startedAt: 1000, cancelArmed: false });
    s = recordingReducer(s, { type: "release", at: 3000 });
    expect(s.phase).toBe("sending");
    expect(recordingReducer(s, { type: "sent" }).phase).toBe("idle");
  });

  it("drops a tap shorter than half a second", () => {
    const rec: RecordingState = { phase: "recording", startedAt: 1000, cancelArmed: false };
    expect(releaseOutcome(rec, 1499)).toBe("too-short");
    expect(releaseOutcome(rec, 1500)).toBe("send");
    expect(recordingReducer(rec, { type: "release", at: 1200 }).phase).toBe("idle");
  });

  it("arms cancel when sliding 80 px away and disarms when coming back", () => {
    const rec: RecordingState = { phase: "recording", startedAt: 0, cancelArmed: false };
    const away = recordingReducer(rec, { type: "move", dx: -80, dy: 0 });
    expect(away).toMatchObject({ cancelArmed: true });
    expect(releaseOutcome(away, 5000)).toBe("cancelled");
    expect(recordingReducer(away, { type: "move", dx: -10, dy: 5 })).toMatchObject({ cancelArmed: false });
  });

  it("ignores a second press and a release before recording starts", () => {
    const starting: RecordingState = { phase: "starting" };
    expect(recordingReducer(starting, { type: "press" })).toBe(starting);
    expect(recordingReducer(starting, { type: "release", at: 0 }).phase).toBe("idle");
    expect(recordingReducer(idle, { type: "started", at: 5 })).toBe(idle);
  });

  it("formats the timer and picks a recording type", () => {
    expect(recordingClock(0)).toBe("0:00");
    expect(recordingClock(59_999)).toBe("0:59");
    expect(recordingClock(60_000)).toBe("1:00");
    expect(pickMimeType((t) => t === "audio/mp4")).toBe("audio/mp4");
    expect(pickMimeType(() => false)).toBe("");
    expect(audioExtension("audio/mp4")).toBe("m4a");
    expect(audioExtension("audio/ogg;codecs=opus")).toBe("ogg");
    expect(audioExtension("audio/webm")).toBe("webm");
  });
});
