/**
 * The advisory page (R3, /app/advisory/:id): reading, personal limit, the fixed
 * advice text and a large Play button for the voice advisory (mp3, or the browser's
 * own voice when the file is missing or fails). The API checks ownership and records
 * that the advisory was opened.
 */
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Pause, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { api, ApiError, type S } from "@/shared/api/client";
import { ConfirmButton } from "@/shared/components/ConfirmButton";
import { ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { mgdl, UUID_RE, when } from "@/shared/lib/format";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";

const HEADLINES: Record<string, string> = {
  pre_breach_high: "Your glucose is close to your upper limit",
  breach_high: "Your glucose is high",
  pre_breach_low: "Your glucose is close to your lower limit",
  breach_low: "Your glucose is low",
};
const BADGES: Record<string, string> = {
  pre_breach_high: "▲ Running high",
  breach_high: "▲ High",
  pre_breach_low: "▼ Running low",
  breach_low: "▼ Low",
};
const CONTEXTS: Record<string, string> = { fasting: "Fasting reading", post_meal: "After-meal reading", random: "Reading" };
// speechSynthesis voice languages per advisory language (skill: en-IN / hi-IN / ta-IN).
const VOICE_LANGS: Record<string, string> = { en: "en-IN", hi: "hi-IN", ta: "ta-IN" };

/** The advisory page. */
export function AdvisoryPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const valid = UUID_RE.test(id);
  const advisory = useQuery({
    queryKey: ["advisory", id],
    queryFn: () => api<S["AdvisoryOut"]>(`/api/advisories/${id}`),
    enabled: valid,
    retry: (count, err) => !(err instanceof ApiError && [403, 404].includes(err.status)) && count < 1,
  });

  if (!valid) return <ErrorState message="This link is not a valid advisory link." />;
  return (
    <div className="space-y-4">
      <Link to="/alerts" className="inline-flex min-h-11 items-center gap-1 font-semibold text-primary">
        <ArrowLeft className="size-4" aria-hidden /> All alerts
      </Link>
      {advisory.isPending && <LoadingBlock rows={3} label="Loading your advice" />}
      {advisory.isError && (
        <ErrorState
          message={advisory.error instanceof ApiError && advisory.error.status === 403 ? `You cannot open this advisory: ${advisory.error.message}.` : advisory.error.message}
          onRetry={() => void advisory.refetch()}
        />
      )}
      {advisory.isSuccess && <Advisory a={advisory.data} onDoctor={() => void navigate(`/?connect=1&advisory_id=${id}`)} />}
    </div>
  );
}

function Advisory({ a, onDoctor }: { a: S["AdvisoryOut"]; onDoctor: () => void }) {
  const breach = a.state.startsWith("breach_") || a.urgent;
  const audio = useRef<HTMLAudioElement>(null);
  const [audioUrl, setAudioUrl] = useState(a.audio_url);
  const [playing, setPlaying] = useState(false);
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);

  useEffect(() => () => window.speechSynthesis?.cancel(), []);

  function speakWithBrowser() {
    if (!("speechSynthesis" in window)) {
      setStatus({ text: "This browser cannot play the voice advice. Please read the text above.", error: true });
      return;
    }
    if (playing) {
      window.speechSynthesis.cancel();
      return;
    }
    const utterance = new SpeechSynthesisUtterance(a.spoken_text);
    utterance.lang = VOICE_LANGS[a.language] ?? "en-IN";
    utterance.rate = 0.95;
    utterance.onstart = () => setPlaying(true);
    utterance.onend = utterance.onerror = () => setPlaying(false);
    window.speechSynthesis.speak(utterance);
  }

  async function play() {
    setStatus(null);
    const el = audio.current;
    if (!audioUrl || !el) {
      speakWithBrowser();
      return;
    }
    if (!el.paused) {
      el.pause();
      el.currentTime = 0;
      return;
    }
    try {
      await el.play();
    } catch {
      // Expired signed URL, network error or unsupported codec: use the browser voice.
      setAudioUrl(null);
      speakWithBrowser();
    }
  }

  const tone = breach ? "text-critical" : "text-warning";
  return (
    <article className="space-y-4">
      <section className="rounded-2xl border bg-card p-5 shadow-sm">
        <span className={cn("inline-block rounded-full px-3 py-0.5 text-[0.85rem] font-bold", breach ? "bg-critical-surface text-critical" : "bg-warning-surface text-warning")}>
          {a.urgent ? "▼ Very low" : BADGES[a.state]}
        </span>
        <h1 className="mt-2 text-2xl leading-tight font-bold">{a.urgent ? "Your glucose is very low" : HEADLINES[a.state]}</h1>
        <dl className="mt-4 grid grid-cols-2 gap-3">
          <div className="rounded-xl bg-muted p-3">
            <dt className="text-[0.9rem] text-muted-foreground">{CONTEXTS[a.context] ?? "Reading"}</dt>
            <dd className={cn("text-4xl font-bold tabular", tone)}>{mgdl(a.reading_value_mg_dl)}</dd>
            <dd className="text-[0.9rem] text-muted-foreground">mg/dL</dd>
          </div>
          <div className="rounded-xl bg-muted p-3">
            <dt className="text-[0.9rem] text-muted-foreground">{a.direction === "high" ? "Your upper limit" : "Your lower limit"}</dt>
            <dd className="text-4xl font-bold tabular">{mgdl(a.threshold_mg_dl)}</dd>
            <dd className="text-[0.9rem] text-muted-foreground">mg/dL</dd>
          </div>
        </dl>
        <p className="mt-3 text-muted-foreground">Measured {when(a.measured_at)}</p>
      </section>

      {a.urgent && (
        <section role="alert" className="rounded-2xl border-2 border-critical bg-critical-surface p-4">
          <p className="flex items-center gap-2 font-bold text-critical">
            <Icons.emergency className="size-5" aria-hidden /> This is a very low reading.
          </p>
          <p className="mt-1">If you feel unwell, confused or faint, call emergency services now.</p>
          <Button asChild size="lg" variant="destructive" className="mt-3">
            <a href="tel:112">Call 112</a>
          </Button>
        </section>
      )}

      <section className="rounded-2xl border bg-card p-5">
        <p id="advice" className="text-lg whitespace-pre-wrap">{a.message_text}</p>
        <Button size="xl" className="mt-4 h-16 w-full text-xl" onClick={() => void play()} aria-describedby="advice">
          {playing ? <Pause className="size-6" aria-hidden /> : <Play className="size-6" aria-hidden />}
          {playing ? "Stop" : "Play voice advice"}
        </Button>
        <audio
          ref={audio}
          src={audioUrl ?? undefined}
          preload="none"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
          onError={() => audioUrl && setAudioUrl(null)}
        />
        <ConfirmButton
          size="lg"
          variant="outline"
          className="mt-3 w-full"
          title="Talk to a doctor?"
          description={<p>This opens the chat and tells your care team about this alert. In an emergency, call 112 instead.</p>}
          confirmLabel="Ask for a doctor"
          onConfirm={onDoctor}
        >
          <Icons.handoff aria-hidden /> Talk to a doctor
        </ConfirmButton>
        <p className="mt-2 text-muted-foreground">Opens the chat and notifies your practitioner. In an emergency, call 112.</p>
        {status && (
          <p role={status.error ? "alert" : "status"} className={cn("mt-3", status.error && "font-semibold text-critical")}>
            {status.text}
          </p>
        )}
      </section>
    </article>
  );
}
