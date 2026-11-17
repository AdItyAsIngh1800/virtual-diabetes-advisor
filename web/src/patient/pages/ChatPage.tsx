/**
 * The chat-first home (R4): typed messages, hold-to-talk voice, spoken replies with
 * a Play button that survives a reload, day separators, handoff banners, quick-reply
 * chips and "Talk to a doctor". Advisor text is shown exactly as the server sends it.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Phone, Play, SendHorizontal } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router";

import { isEmergencyReply, recordingClock, splitSource, spokenText } from "@/patient/lib/chat";
import { useRecorder } from "@/patient/useRecorder";
import { api, type S } from "@/shared/api/client";
import { useAuth } from "@/shared/auth/AuthProvider";
import { listen } from "@/shared/auth/supabase";
import { ConfirmButton } from "@/shared/components/ConfirmButton";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { clock, isDay, messageId, UUID_RE, withDays } from "@/shared/lib/format";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";
import { Textarea } from "@/shared/ui/textarea";

type Message = S["ChatMessageOut"];
type Handoff = S["HandoffOut"];
interface Spoken {
  url: string | null;
  text: string;
}

// Fallback refresh while a practitioner conversation is live, in case Realtime drops.
const POLL_MS = 20_000;
const WHO: Record<string, string> = { assistant: "Advisor", practitioner: "Practitioner" };
// speechSynthesis voice for spoken replies (skill: en-IN / hi-IN / ta-IN).
const VOICE_LANG = "en-IN";

/** The patient's chat page. */
export function ChatPage() {
  const { state } = useAuth();
  const uid = state.status === "signed-in" ? state.session.user.id : null;
  const queryClient = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);
  const [text, setText] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const spoken = useRef(new Map<string, Spoken>());
  const player = useRef<HTMLAudioElement>(null);
  const textRef = useRef<HTMLTextAreaElement>(null);
  const lastSeen = useRef<string | null>(null);

  const conversation = useQuery({
    queryKey: ["conversation"],
    queryFn: () => api<S["ConversationOut"]>("/api/chat/messages"),
    refetchInterval: (q) => (q.state.data?.handoff ? POLL_MS : false),
  });
  const messages = conversation.data?.messages ?? [];
  const handoff = conversation.data?.handoff ?? null;

  // Practitioner replies arrive on the private chat:<auth uid> topic.
  useEffect(() => {
    if (!uid) return;
    let alive = true;
    let leave: (() => void) | null = null;
    void listen(`chat:${uid}`, "message", () => void queryClient.invalidateQueries({ queryKey: ["conversation"] })).then((fn) => {
      if (alive) leave = fn;
      else fn();
    });
    return () => {
      alive = false;
      leave?.();
    };
  }, [uid, queryClient]);

  // Announce a new advisor or practitioner reply to screen readers (once).
  useEffect(() => {
    const last = messages.at(-1);
    if (!last) return;
    if (lastSeen.current !== null && last.id !== lastSeen.current && last.role !== "patient") {
      setAnnounce(`${WHO[last.role] ?? "Notice"}: ${last.text}`);
    }
    lastSeen.current = last.id;
  }, [messages]);

  const speakWithBrowser = useCallback((reply: string) => {
    if (!("speechSynthesis" in window)) {
      setStatus({ text: "This browser cannot play voice replies. Please read the text.", error: true });
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(reply);
    utterance.lang = VOICE_LANG;
    utterance.rate = 0.95;
    window.speechSynthesis.speak(utterance);
  }, []);

  /** Play a spoken reply; fall back to the browser voice if the mp3 fails. */
  const playReply = useCallback(
    async (audio: Spoken) => {
      const words = spokenText(audio.text);
      const el = player.current;
      if (!audio.url || !el) {
        speakWithBrowser(words);
        return;
      }
      el.src = audio.url;
      try {
        await el.play();
      } catch (err) {
        if (err instanceof DOMException && err.name === "NotAllowedError") {
          // Autoplay blocked (iPhone after an await): keep the mp3 for the Play button.
          setStatus({ text: "Tap ▶ Play to hear the reply.", error: false });
          return;
        }
        audio.url = null; // expired signed URL or a media error: use the browser voice
        speakWithBrowser(words);
      }
    },
    [speakWithBrowser],
  );

  const send = useMutation({
    mutationFn: async ({ form }: { form: FormData; label: string }) => {
      form.append("message_id", messageId());
      return api<S["ChatReplyOut"]>("/api/chat", "POST", form);
    },
    onMutate: ({ label }) => {
      setStatus(null);
      setPending(label);
    },
    onSuccess: (reply) => {
      if (reply.transcript !== null && reply.transcript !== undefined) {
        const audio: Spoken = { url: reply.audio_url ?? null, text: reply.reply };
        spoken.current.set(`${reply.turn_id}:a`, audio);
        // The patient just interacted, so most browsers allow playing straight away.
        void playReply(audio);
      }
    },
    onError: (err) => setStatus({ text: err.message, error: true }),
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["conversation"] });
      setPending(null);
      recorder.done();
    },
  });
  const busy = send.isPending;

  const recorder = useRecorder({
    disabled: busy,
    onRecorded: (blob, filename) => {
      const form = new FormData();
      form.append("audio", blob, filename);
      send.mutate({ form, label: "🎤 Voice message…" });
    },
    onMessage: (t, error) => setStatus({ text: t, error }),
  });

  const sendText = (event?: FormEvent) => {
    event?.preventDefault();
    const value = text.trim();
    if (!value || busy) return;
    const form = new FormData();
    form.append("text", value);
    setText("");
    send.mutate({ form, label: value });
  };

  const talkToDoctor = useCallback(
    (advisoryId?: string) => {
      const form = new FormData();
      form.append("request_practitioner", "true");
      form.append("text", "Talk to a doctor");
      if (advisoryId && UUID_RE.test(advisoryId)) form.append("advisory_id", advisoryId);
      send.mutate({ form, label: "Talk to a doctor" });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- send.mutate is stable
    [],
  );

  // From the advisory page's "Talk to a doctor": /app/?connect=1&advisory_id=<id>.
  const connectHandled = useRef(false);
  useEffect(() => {
    if (params.get("connect") !== "1" || connectHandled.current || !conversation.isSuccess) return;
    connectHandled.current = true;
    const advisoryId = params.get("advisory_id") ?? undefined;
    setParams({}, { replace: true });
    talkToDoctor(advisoryId);
  }, [params, setParams, conversation.isSuccess, talkToDoctor]);

  // Keep the newest message in view.
  const end = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, pending]);

  const spokenFor = (m: Message): Spoken | null => {
    if (m.role !== "assistant") return null;
    const known = spoken.current.get(m.id);
    if (m.voice_reply) {
      // A fresh signed URL from the server replaces an expired or missing one.
      const audio = known ?? { url: null, text: m.text };
      if (m.audio_url) audio.url = m.audio_url;
      spoken.current.set(m.id, audio);
      return audio;
    }
    return known ?? null;
  };

  const quick = (prefix: string) => {
    setText(prefix);
    requestAnimationFrame(() => {
      const el = textRef.current;
      if (el) {
        el.focus();
        el.setSelectionRange(prefix.length, prefix.length);
      }
    });
  };

  const recording = recorder.state.phase === "recording";
  const cancelArmed = recording && recorder.state.phase === "recording" && recorder.state.cancelArmed;

  return (
    <div className="flex flex-1 flex-col">
      <h1 className="sr-only">Chat with your advisor</h1>
      <HandoffBanner handoff={handoff} />

      <div className="flex-1 space-y-3 pb-4" aria-label="Conversation" role="log">
        {conversation.isPending && <LoadingBlock rows={4} label="Loading your conversation" />}
        {conversation.isError && <ErrorState message={conversation.error.message} onRetry={() => void conversation.refetch()} />}
        {conversation.isSuccess && messages.length === 0 && !pending && (
          <EmptyState icon={Icons.chat} title="Ask anything about your glucose">
            Send a reading like “fasting 132” or “190 after lunch”, or ask a question. You can also hold the microphone and talk.
          </EmptyState>
        )}
        {withDays(messages).map((item) =>
          isDay(item) ? (
            <div key={item.key} role="separator" aria-label={item.day} className="flex justify-center">
              <span className="rounded-full border bg-card px-3 py-0.5 text-[0.8rem] font-semibold text-muted-foreground">{item.day}</span>
            </div>
          ) : (
            <Bubble key={item.id} message={item} audio={spokenFor(item)} onPlay={(a) => void playReply(a)} />
          ),
        )}
        {pending && (
          <>
            <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-bubble-me px-4 py-2.5 whitespace-pre-wrap text-bubble-me-foreground opacity-80">{pending}</div>
            <p className="text-muted-foreground" role="status">
              {handoff?.status === "assigned" ? "Sending to your practitioner…" : "Advisor is replying…"}
            </p>
          </>
        )}
        <div ref={end} />
      </div>

      <p className="sr-only" aria-live="polite">{announce}</p>

      {/* Sticky composer above the bottom navigation. */}
      <div className="sticky bottom-[calc(4rem+env(safe-area-inset-bottom))] z-10 -mx-4 border-t bg-background/95 px-4 pt-3 pb-3 supports-[backdrop-filter]:bg-background/80 supports-[backdrop-filter]:backdrop-blur">
        <div className="mb-2 flex gap-2 overflow-x-auto pb-1" role="group" aria-label="Quick replies">
          <Button variant="outline" size="sm" className="h-10 shrink-0 rounded-full" onClick={() => quick("fasting ")}>
            <Icons.reading aria-hidden /> Log fasting reading
          </Button>
          <Button variant="outline" size="sm" className="h-10 shrink-0 rounded-full" onClick={() => quick("after meal ")}>
            <Icons.reading aria-hidden /> Log after-meal reading
          </Button>
          <ConfirmButton
            variant="outline"
            size="sm"
            className="h-10 shrink-0 rounded-full"
            disabled={busy}
            title="Talk to a doctor?"
            description={<p>Your care team will be told and can see this chat. Until a practitioner joins, the Advisor keeps answering. In an emergency, call 112 instead.</p>}
            confirmLabel="Ask for a doctor"
            onConfirm={() => talkToDoctor()}
          >
            <Icons.handoff aria-hidden /> Talk to a doctor
          </ConfirmButton>
        </div>

        <form onSubmit={sendText} className="flex items-end gap-2">
          <Textarea
            ref={textRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) sendText(e);
            }}
            rows={1}
            maxLength={2000}
            disabled={busy}
            placeholder="Reading or question"
            aria-label="Message"
            className="max-h-32 min-h-12 flex-1 resize-none rounded-2xl text-base"
          />
          {text.trim() ? (
            <Button type="submit" size="icon-lg" className="size-14 rounded-full" disabled={busy} aria-label="Send">
              <SendHorizontal className="size-6" aria-hidden />
            </Button>
          ) : (
            <button
              type="button"
              {...recorder.handlers}
              disabled={busy}
              aria-label="Hold to record a voice message"
              aria-pressed={recording}
              aria-describedby="rec-hint"
              className={cn(
                "grid size-14 shrink-0 touch-none place-items-center rounded-full bg-primary text-primary-foreground shadow-md select-none transition-transform disabled:opacity-50",
                recording && "scale-110 bg-destructive text-destructive-foreground ring-4 ring-destructive/30",
                cancelArmed && "bg-muted text-foreground ring-muted-foreground/40",
              )}
              style={{ WebkitTouchCallout: "none", WebkitUserSelect: "none" }}
            >
              <Icons.voice className="size-6" aria-hidden />
            </button>
          )}
        </form>
        <p id="rec-hint" aria-live="polite" className={cn("mt-1.5 min-h-6 text-[0.9rem] text-muted-foreground", recording && "font-semibold text-critical", cancelArmed && "text-foreground")}>
          {recording
            ? cancelArmed
              ? `Release to cancel · ${recordingClock(recorder.elapsed)}`
              : `Recording ${recordingClock(recorder.elapsed)} of 1:00 · release to send, slide away to cancel`
            : recorder.state.phase === "starting"
              ? "Opening the microphone…"
              : "Hold the microphone to talk (up to a minute)."}
        </p>
        {status && (
          <p role={status.error ? "alert" : "status"} className={cn("text-[0.95rem]", status.error ? "font-semibold text-critical" : "text-foreground")}>
            {status.text}
          </p>
        )}
      </div>
      <audio ref={player} preload="none" className="hidden" />
    </div>
  );
}

/** Waiting / "<name> is in this chat" / no practitioner linked yet. */
function HandoffBanner({ handoff }: { handoff: Handoff | null }) {
  if (!handoff) return null;
  let text: string;
  if (handoff.status === "assigned") {
    text = `${handoff.practitioner_name || "Your practitioner"} is in this chat. Your messages go to them.`;
  } else if (!handoff.has_practitioner) {
    text = "Your request is saved for the care team. No practitioner is linked yet, so please also contact your doctor directly.";
  } else {
    text = "Waiting for a practitioner to join. Until then the Advisor keeps answering, and they will see this chat.";
  }
  return (
    <div role="status" className="mb-3 flex items-start gap-2 rounded-2xl bg-info-surface p-3 text-info">
      <Icons.handoff className="mt-0.5 size-5 shrink-0" aria-hidden />
      <p className="font-medium">{text}</p>
    </div>
  );
}

/** One message. Text is rendered as text (never HTML) and never shortened. */
function Bubble({ message: m, audio, onPlay }: { message: Message; audio: Spoken | null; onPlay: (a: Spoken) => void }) {
  if (m.role === "system") {
    return (
      <p className="text-center text-[0.9rem] text-muted-foreground italic">
        {m.text} · {clock(m.created_at)}
      </p>
    );
  }
  const meta = (
    <span className="mt-1 flex items-center gap-1 text-[0.8rem] opacity-80">
      {m.spoken && <Icons.voice className="size-3.5" aria-label="Spoken" />}
      <time dateTime={m.created_at}>{clock(m.created_at)}</time>
    </span>
  );
  const play = audio && (
    <Button variant="outline" size="sm" className="mt-2 h-10 rounded-full bg-card text-foreground" onClick={() => onPlay(audio)} aria-label="Play the spoken reply">
      <Play aria-hidden /> Play
    </Button>
  );

  if (m.role === "patient") {
    return (
      <div className="ml-auto max-w-[85%] rounded-2xl rounded-br-md bg-bubble-me px-4 py-2.5 text-bubble-me-foreground">
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{m.text}</p>
        {meta}
      </div>
    );
  }

  if (isEmergencyReply(m.role, m.text)) {
    return (
      <div role="alert" className="max-w-[92%] rounded-2xl border-2 border-critical bg-critical-surface p-4 text-foreground">
        <p className="mb-1 flex items-center gap-1.5 text-[0.85rem] font-bold text-critical">
          <Icons.emergency className="size-4" aria-hidden /> Advisor · urgent
        </p>
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{m.text}</p>
        <Button asChild size="lg" variant="destructive" className="mt-3">
          <a href="tel:112">
            <Phone aria-hidden /> Call 112
          </a>
        </Button>
        {play}
        {meta}
      </div>
    );
  }

  const { body, source } = splitSource(m.text);
  const doctor = m.role === "practitioner";
  return (
    <div className={cn("max-w-[88%] rounded-2xl rounded-bl-md border px-4 py-2.5", doctor ? "bg-bubble-doctor text-bubble-doctor-foreground" : "bg-bubble-advisor")}>
      <p className="mb-0.5 text-[0.8rem] font-bold opacity-80">{WHO[m.role] ?? m.role}</p>
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{body}</p>
      {source && <p className="mt-2 border-t pt-2 text-[0.85rem] whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{source}</p>}
      {play}
      {meta}
    </div>
  );
}
