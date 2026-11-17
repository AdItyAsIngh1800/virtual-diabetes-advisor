/**
 * Hold-to-record with MediaRecorder: press and hold to talk, release to send, slide
 * away to cancel, a 60 s cap. The state machine is recordingReducer (tested); this
 * hook wires it to the microphone and the pointer/keyboard events.
 */
import { useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState } from "react";

import {
  MAX_RECORDING_MS,
  audioExtension,
  pickMimeType,
  recordingReducer,
  releaseOutcome,
  type RecordingOutcome,
  type RecordingState,
} from "@/patient/lib/chat";

interface Options {
  /** A finished recording to upload. */
  onRecorded: (blob: Blob, filename: string) => void;
  /** A hint or error for the status line. */
  onMessage: (text: string, isError: boolean) => void;
  disabled: boolean;
}

interface Media {
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: Blob[];
}

/** Recording state, elapsed time and the handlers for the record button. */
export function useRecorder({ onRecorded, onMessage, disabled }: Options) {
  const [state, dispatch] = useReducer(recordingReducer, { phase: "idle" } as RecordingState);
  const stateRef = useRef(state);
  useLayoutEffect(() => {
    stateRef.current = state;
  }, [state]);
  const media = useRef<Media | null>(null);
  const outcome = useRef<RecordingOutcome>("cancelled");
  const origin = useRef({ x: 0, y: 0 });
  const [elapsed, setElapsed] = useState(0);
  const cap = useRef<ReturnType<typeof setTimeout> | null>(null);
  const callbacks = useRef({ onRecorded, onMessage });
  useLayoutEffect(() => {
    callbacks.current = { onRecorded, onMessage };
  }, [onRecorded, onMessage]);

  const stop = useCallback((cancel: boolean) => {
    const m = media.current;
    if (!m || m.recorder.state === "inactive") {
      if (stateRef.current.phase === "starting") dispatch({ type: "release", at: Date.now() });
      return;
    }
    const at = Date.now();
    outcome.current = cancel ? "cancelled" : releaseOutcome(stateRef.current, at);
    dispatch(cancel ? { type: "cancel" } : { type: "release", at });
    m.recorder.stop();
  }, []);

  const start = useCallback(async () => {
    if (disabled || stateRef.current.phase !== "idle") return;
    if (!navigator.mediaDevices || typeof MediaRecorder === "undefined") {
      callbacks.current.onMessage("Voice messages are not supported in this browser. Please type instead.", true);
      return;
    }
    dispatch({ type: "press" });
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      dispatch({ type: "failed" });
      callbacks.current.onMessage("Microphone access was refused. Allow it in the browser's site settings.", true);
      return;
    }
    // Re-read: the ref may have changed while the microphone was opening.
    // (Read through a function so TypeScript does not keep the narrowing from above.)
    const read = (): RecordingState => stateRef.current;
    if (read().phase !== "starting") {
      // Released before the microphone opened: nothing to record.
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    const mimeType = pickMimeType((t) => MediaRecorder.isTypeSupported(t));
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    const m: Media = { recorder, stream, chunks: [] };
    media.current = m;
    recorder.ondataavailable = (e) => {
      if (e.data.size) m.chunks.push(e.data);
    };
    recorder.onstop = () => {
      if (cap.current) clearTimeout(cap.current);
      stream.getTracks().forEach((t) => t.stop());
      media.current = null;
      const type = recorder.mimeType || "audio/webm";
      if (outcome.current === "send" && m.chunks.length) {
        callbacks.current.onRecorded(new Blob(m.chunks, { type }), `voice.${audioExtension(type)}`);
      } else {
        dispatch({ type: "cancel" });
        if (outcome.current === "too-short") callbacks.current.onMessage("Hold the microphone button while you speak, then let go.", false);
        else callbacks.current.onMessage("Recording cancelled.", false);
      }
    };
    recorder.start();
    dispatch({ type: "started", at: Date.now() });
    setElapsed(0);
    cap.current = setTimeout(() => stop(false), MAX_RECORDING_MS);
  }, [disabled, stop]);

  useEffect(() => {
    if (state.phase !== "recording") return;
    const t = setInterval(() => setElapsed(Date.now() - state.startedAt), 250);
    return () => clearInterval(t);
  }, [state]);

  // Never leave the microphone on when the page goes away.
  useEffect(() => () => {
    const m = media.current;
    if (m && m.recorder.state !== "inactive") {
      outcome.current = "cancelled";
      m.recorder.stop();
    }
  }, []);

  const handlers = {
    onPointerDown: (e: React.PointerEvent<HTMLButtonElement>) => {
      e.preventDefault();
      e.currentTarget.setPointerCapture(e.pointerId);
      origin.current = { x: e.clientX, y: e.clientY };
      void start();
    },
    onPointerMove: (e: React.PointerEvent<HTMLButtonElement>) => {
      if (stateRef.current.phase === "recording") {
        dispatch({ type: "move", dx: e.clientX - origin.current.x, dy: e.clientY - origin.current.y });
      }
    },
    onPointerUp: () => stop(false),
    onPointerCancel: () => stop(true),
    onContextMenu: (e: React.MouseEvent) => e.preventDefault(),
    onKeyDown: (e: React.KeyboardEvent) => {
      if ((e.key === " " || e.key === "Enter") && !e.repeat) {
        e.preventDefault();
        void start();
      } else if (e.key === "Escape") {
        stop(true);
      }
    },
    onKeyUp: (e: React.KeyboardEvent) => {
      if (e.key === " " || e.key === "Enter") stop(false);
    },
  };

  return { state, elapsed, handlers, done: () => dispatch({ type: "sent" }) };
}
