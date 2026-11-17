/**
 * Review (R5): what the automatic advisor told the practitioner's patients, with a
 * verdict on each. A note is required unless the verdict is Correct. Reviews are
 * append-only: a new review becomes the current verdict and the history is kept.
 */
import { useState } from "react";

import { useReview, useTurns } from "@/practitioner/data";
import { REVIEW_FILTERS, VERDICTS, needsNote, reviewTags, sourceLine, type ReviewFilter, type Verdict } from "@/practitioner/lib/dashboard";
import type { S } from "@/shared/api/client";
import { PageHeader } from "@/shared/components/PageHeader";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { firstName, when } from "@/shared/lib/format";
import { useStored } from "@/shared/lib/storage";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";
import { Label } from "@/shared/ui/label";
import { Textarea } from "@/shared/ui/textarea";

const isFilter = (v: unknown): v is ReviewFilter => REVIEW_FILTERS.some(([f]) => f === v);

export function ReviewPage() {
  const [filter, setFilter] = useStored<ReviewFilter>("practitioner:review-filter", "unreviewed", isFilter);
  const turns = useTurns(filter);
  const items = turns.data?.pages.flatMap((p) => p.items) ?? [];
  return (
    <>
      <PageHeader title="Review advisor answers" updatedAt={turns.dataUpdatedAt} onRefresh={() => void turns.refetch()} refreshing={turns.isFetching}>
        What the automatic advisor told your patients. Mark each answer; a note is required unless it is correct. Reviews are never edited: a new review becomes the current verdict and the history is kept.
      </PageHeader>
      <div role="group" aria-label="Show" className="mb-4 flex flex-wrap gap-2">
        {REVIEW_FILTERS.map(([f, label]) => (
          <Button key={f} size="sm" variant={filter === f ? "default" : "outline"} aria-pressed={filter === f} onClick={() => setFilter(f)} className="rounded-full">
            {label}
          </Button>
        ))}
      </div>
      {turns.isPending && <LoadingBlock rows={3} label="Loading answers" />}
      {turns.isError && <ErrorState message={turns.error.message} onRetry={() => void turns.refetch()} />}
      {turns.isSuccess && items.length === 0 && (
        <EmptyState icon={Icons.review} title="Nothing to review under this filter.">
          {filter !== "all" ? "Choose All to see every answer." : "Answers appear here after patients use the chat."}
        </EmptyState>
      )}
      <ul className="space-y-4" aria-label="Advisor answers">
        {items.map((t) => (
          <TurnCard key={t.turn_id} turn={t} />
        ))}
      </ul>
      {turns.hasNextPage && (
        <Button variant="outline" className="mt-4" onClick={() => void turns.fetchNextPage()} disabled={turns.isFetchingNextPage}>
          {turns.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
    </>
  );
}

function TurnCard({ turn: initial }: { turn: S["ReviewableTurn"] }) {
  // The saved verdict replaces the card's current one locally, without a refetch
  // that would move the card out from under the reader.
  const [turn, setTurn] = useState(initial);
  const [note, setNote] = useState("");
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);
  const review = useReview(turn.turn_id);
  const noteId = `note-${turn.turn_id}`;
  const r = turn.current_review;

  function submit(verdict: Verdict) {
    const text = note.trim();
    if (needsNote(verdict) && !text) {
      setStatus({ text: `Add a note before marking this "${VERDICTS[verdict]}".`, error: true });
      document.getElementById(noteId)?.focus();
      return;
    }
    setStatus(null);
    review.mutate(
      { verdict, note: text || null },
      {
        onSuccess: (record) => {
          setTurn({ ...turn, current_review: record, review_count: turn.review_count + 1 });
          setNote("");
          setStatus({ text: `Saved: ${VERDICTS[verdict]}.`, error: false });
        },
        onError: (err) => setStatus({ text: err.message, error: true }),
      },
    );
  }

  return (
    <li className="rounded-2xl border bg-card p-4 sm:p-5">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{turn.patient_first_name || "Patient"}</span>
        <span className="text-sm text-muted-foreground">{when(turn.created_at)}</span>
        {reviewTags(turn).map((tag) => (
          <Tag key={tag.text} tone={tag.urgent ? "urgent" : "neutral"}>{tag.text}</Tag>
        ))}
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground">{turn.spoken ? "Patient said (voice)" : "Patient asked"}</h3>
          <p className="mt-1 rounded-xl border bg-background/40 p-3 whitespace-pre-wrap">{turn.question || "(no text: the Talk to a doctor button)"}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground">Advisor answered</h3>
          <p className="mt-1 rounded-xl bg-bubble-advisor p-3 whitespace-pre-wrap">{turn.answer}</p>
        </div>
      </div>
      <h3 className="mt-3 text-sm font-semibold text-muted-foreground">Source entry</h3>
      {turn.sources.length ? (
        <ul className="mt-1 list-disc pl-5 text-sm">
          {turn.sources.map((s) => (
            <li key={s.id}>{sourceLine(s)}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-1 text-sm text-muted-foreground">
          {turn.unanswered
            ? "None: no knowledge-base entry was close enough, so the advisor said it did not know."
            : "None: a fixed rule answered (reading, red flag, dosing guard or greeting)."}
        </p>
      )}
      <p className="mt-2 text-sm text-muted-foreground">
        {turn.intent || "other"} · {turn.model || "unknown model"}
        {turn.retrieval_confidence !== null && ` · similarity ${turn.retrieval_confidence.toFixed(2)}`}
      </p>
      {r && (
        <p className="mt-3 rounded-xl bg-muted p-3 text-sm">
          Current verdict: <strong>{VERDICTS[r.verdict]}</strong> by {firstName(r.reviewer_name, "a practitioner")}, {when(r.created_at)}
          {r.note ? `: "${r.note}"` : ""}
          {turn.review_count > 1 ? ` (${turn.review_count} reviews in total)` : ""}
        </p>
      )}
      <div className="mt-3 space-y-1.5">
        <Label htmlFor={noteId}>Note (required unless Correct: what is wrong, or what the knowledge base should say)</Label>
        <Textarea id={noteId} rows={2} maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} aria-describedby={`${noteId}-status`} />
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        {(Object.keys(VERDICTS) as Verdict[]).map((v) => (
          <Button
            key={v}
            size="sm"
            variant={v === "correct" ? "default" : v === "unsafe" ? "destructive" : "outline"}
            disabled={review.isPending}
            onClick={() => submit(v)}
          >
            {VERDICTS[v]}
          </Button>
        ))}
      </div>
      <p id={`${noteId}-status`} role="status" className={cn("mt-2 text-sm", status?.error ? "font-semibold text-critical" : "text-muted-foreground")}>
        {status?.text}
      </p>
    </li>
  );
}
