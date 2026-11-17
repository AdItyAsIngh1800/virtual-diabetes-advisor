/**
 * Knowledge base (R5): drafts awaiting approval (priority first: answers marked
 * unsafe) and the approved entries. The system only proposes questions; a
 * practitioner writes the answer, cites its source and approves it. Approving needs
 * the text and a citation; approved entries are never changed in place (Edit opens a
 * draft copy). review_hint is staff-only and is never shown to patients.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { useAudit, useKb, useKbActions } from "@/practitioner/data";
import { KB_PRIORITY_LABEL, kbApproveProblem, kbDraftOrigin, kbDraftTags, kbSortDrafts, kbStatusLine } from "@/practitioner/lib/dashboard";
import type { S } from "@/shared/api/client";
import { ConfirmButton } from "@/shared/components/ConfirmButton";
import { PageHeader } from "@/shared/components/PageHeader";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { firstName, when } from "@/shared/lib/format";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Label } from "@/shared/ui/label";
import { Textarea } from "@/shared/ui/textarea";

// Mirrors SEED_PRACTITIONER_NAME in app/db/kb_repository.py.
const SEED_ACCOUNT = "Demo Practitioner";
const SEED_LINE = "Approved by the seed account, not yet reviewed by a practitioner.";

export function KbPage() {
  const kb = useKb();
  const audit = useAudit("", "");
  const focusId = useRef<string | null>(null);

  // After Edit (or "Go to open draft"), bring the draft into view and focus its text
  // (now, or once the refreshed list has rendered it).
  const focusDraft = useCallback(() => {
    if (!focusId.current) return;
    const card = document.getElementById(`kb-${focusId.current}`);
    if (!card) return;
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    card.querySelector("textarea")?.focus({ preventScroll: true });
    focusId.current = null;
  }, []);
  useEffect(focusDraft, [kb.data, focusDraft]);
  const onDraft = (id: string) => {
    focusId.current = id;
    focusDraft();
  };

  return (
    <>
      <PageHeader title="Knowledge base" updatedAt={kb.dataUpdatedAt} onRefresh={() => void kb.refetch()} refreshing={kb.isFetching}>
        What the advisor answers from. Nothing reaches patients until a practitioner approves it, and approved entries are never changed in place.
      </PageHeader>
      {audit.data && (
        <p className="mb-6 rounded-2xl border-l-4 border-warning bg-warning-surface p-4 text-warning" role="note">
          {kbStatusLine(audit.data.kb_status)}
        </p>
      )}
      {kb.isPending && <LoadingBlock rows={3} label="Loading the knowledge base" />}
      {kb.isError && <ErrorState message={kb.error.message} onRetry={() => void kb.refetch()} />}
      {kb.isSuccess && (
        <>
          <section aria-labelledby="drafts-title">
            <h2 id="drafts-title" className="text-lg font-bold">Drafts awaiting approval ({kb.data.drafts.length})</h2>
            <p className="mt-1 mb-3 max-w-3xl text-muted-foreground">
              Questions the advisor got wrong or could not answer. The system only proposes the question: you write the answer, cite its source and approve it.
            </p>
            {kb.data.drafts.length === 0 ? (
              <EmptyState icon={Icons.kb} title="No drafts are waiting.">The weekly review proposes drafts from answers marked wrong or unanswered. Edit an approved entry to start one yourself.</EmptyState>
            ) : (
              <ul className="space-y-4">
                {kbSortDrafts(kb.data.drafts).map((d) => (
                  <DraftCard key={`${d.id}-${d.updated_at}`} entry={d} />
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="approved-title" className="mt-10">
            <h2 id="approved-title" className="text-lg font-bold">Approved entries ({kb.data.approved.length})</h2>
            <p className="mt-1 mb-3 max-w-3xl text-muted-foreground">
              What the advisor answers from now. Edit opens a draft copy; the approved text keeps being served until you approve the new version. Approving an unchanged copy re-approves the entry in your name.
            </p>
            {kb.data.approved.length === 0 ? (
              <EmptyState icon={Icons.kb} title="No approved entries yet." />
            ) : (
              <ul className="grid gap-3 lg:grid-cols-2">
                {kb.data.approved.map((e) => (
                  <ApprovedCard key={e.id} entry={e} onDraft={onDraft} />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </>
  );
}

function DraftCard({ entry }: { entry: S["KbEntryView"] }) {
  const actions = useKbActions();
  const [title, setTitle] = useState(entry.title);
  const [body, setBody] = useState(entry.body);
  const [citation, setCitation] = useState(entry.source_citation ?? "");
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);
  const draft: S["KbDraftIn"] = { title: title.trim(), body: body.trim(), source_citation: citation.trim() || null };
  const problem = kbApproveProblem(draft);
  const busy = actions.save.isPending || actions.approve.isPending || actions.reject.isPending;
  const id = entry.id;
  const fail = (err: Error) => setStatus({ text: err.message, error: true });

  return (
    <li id={`kb-${id}`} className={cn("scroll-mt-20 rounded-2xl border bg-card p-4 sm:p-5", entry.priority && "border-l-4 border-l-critical")}>
      <div className="flex flex-wrap items-center gap-2">
        {kbDraftTags(entry).map((t) => (
          <Tag key={t.text} tone={t.urgent ? "urgent" : "neutral"}>{t.text}</Tag>
        ))}
        <span className="font-semibold">{kbDraftOrigin(entry)}</span>
        <span className="text-sm text-muted-foreground">{when(entry.created_at)}</span>
      </div>
      {entry.priority && (
        <p className="mt-3 font-semibold text-critical">{KB_PRIORITY_LABEL}: the advisor's answer may have put a patient at risk. Write the correct answer first.</p>
      )}
      {entry.proposed_from_turn_id && (
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {entry.source_turn ? (
            <>
              <div>
                <h3 className="text-sm font-semibold text-muted-foreground">Patient asked</h3>
                <p className="mt-1 rounded-xl border bg-background/40 p-3 whitespace-pre-wrap">{entry.source_turn.question || "(no text)"}</p>
              </div>
              <div>
                <h3 className="text-sm font-semibold text-muted-foreground">Advisor answered</h3>
                <p className="mt-1 rounded-xl bg-bubble-advisor p-3 whitespace-pre-wrap">{entry.source_turn.answer || "(no reply)"}</p>
                <p className="mt-1 text-sm text-muted-foreground">Asked {when(entry.source_turn.asked_at)}</p>
              </div>
            </>
          ) : (
            <p className="text-sm text-muted-foreground md:col-span-2">Asked by a patient who is not assigned to you; the draft title below is their question.</p>
          )}
        </div>
      )}
      {entry.review_hint && (
        <div className="mt-3 rounded-xl border-l-4 border-warning bg-warning-surface p-3 text-warning">
          <h3 className="text-sm font-bold">Why it was proposed (staff only, never shown to patients)</h3>
          <p className="mt-1 whitespace-pre-wrap">{entry.review_hint}</p>
        </div>
      )}

      <div className="mt-4 space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor={`kb-title-${id}`}>Title (patients see this)</Label>
          <Input id={`kb-title-${id}`} value={title} maxLength={2000} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`kb-body-${id}`}>Entry text (patients see this)</Label>
          <Textarea id={`kb-body-${id}`} rows={6} maxLength={6000} value={body} onChange={(e) => setBody(e.target.value)} placeholder="Write the answer in plain language, as you would say it to a patient." />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor={`kb-cite-${id}`}>Source citation (required to approve)</Label>
          <Input id={`kb-cite-${id}`} value={citation} maxLength={500} onChange={(e) => setCitation(e.target.value)} placeholder="e.g. ADA Standards of Care in Diabetes-2026, section 6" />
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            actions.save.mutate(
              { id, draft },
              { onSuccess: () => setStatus({ text: "Draft saved. It is not served until approved.", error: false }), onError: fail },
            )
          }
        >
          Save draft
        </Button>
        {problem ? (
          <Button disabled={busy} onClick={() => setStatus({ text: problem, error: true })}>
            Approve
          </Button>
        ) : (
          <ConfirmButton
            title="Approve this entry?"
            description={<p>Patients will get this text from their next question. It is approved in your name and replaces the entry it edits.</p>}
            confirmLabel="Approve"
            disabled={busy}
            onConfirm={() =>
              actions.approve.mutate(
                { id, draft },
                { onSuccess: (v) => toast.success(`Approved as knowledge base version ${v.kb_version ?? "?"}.`), onError: fail },
              )
            }
          >
            Approve
          </ConfirmButton>
        )}
        <ConfirmButton
          variant="outline"
          destructive
          title="Reject this draft?"
          description={<p>It is kept for the record but never served.</p>}
          confirmLabel="Reject"
          disabled={busy}
          onConfirm={() => actions.reject.mutate(id, { onSuccess: () => toast.success("Draft rejected."), onError: fail })}
          className="text-critical"
        >
          Reject
        </ConfirmButton>
      </div>
      <p role="status" className={cn("mt-2 text-sm", status?.error ? "font-semibold text-critical" : "text-muted-foreground")}>
        {status?.text}
      </p>
    </li>
  );
}

function ApprovedCard({ entry, onDraft }: { entry: S["KbEntryView"]; onDraft: (id: string) => void }) {
  const actions = useKbActions();
  const [error, setError] = useState<string | null>(null);
  const seed = entry.approved_by_name === SEED_ACCOUNT;
  return (
    <li className="rounded-2xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-semibold">{entry.title}</span>
        <Tag>v{entry.kb_version}</Tag>
        {entry.open_draft_id && <Tag tone="warn">Draft open</Tag>}
      </div>
      <p className="mt-1 text-sm text-muted-foreground">
        {entry.entry_key} · {entry.source_citation || "no citation"}
      </p>
      <p className="text-sm text-muted-foreground">
        Approved{entry.approved_by_name && !seed ? ` by ${firstName(entry.approved_by_name, "a practitioner")}` : ""}
        {entry.approved_at ? `, ${when(entry.approved_at)}` : ""}
      </p>
      {seed && <p className="mt-1 text-sm font-semibold text-warning">{SEED_LINE}</p>}
      <details className="mt-2">
        <summary className="cursor-pointer text-sm font-medium text-primary">Entry text</summary>
        <p className="mt-2 rounded-xl bg-muted p-3 whitespace-pre-wrap">{entry.body}</p>
      </details>
      <Button
        variant="outline"
        size="sm"
        className="mt-3"
        disabled={actions.edit.isPending}
        onClick={() => {
          if (entry.open_draft_id) {
            onDraft(entry.open_draft_id);
            return;
          }
          actions.edit.mutate(entry.id, { onSuccess: (d) => onDraft(d.id), onError: (err) => setError(err.message) });
        }}
      >
        {entry.open_draft_id ? "Go to open draft" : "Edit"}
      </Button>
      {error && (
        <p role="alert" className="mt-2 text-sm font-semibold text-critical">
          {error}
        </p>
      )}
    </li>
  );
}
