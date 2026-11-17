/**
 * Handoffs (R4): patients who asked for a practitioner, a red flag, or an escalated
 * alert. A list and the selected thread side by side on desktop (one at a time on a
 * phone); claim, reply and resolve, each confirmed where it changes who answers.
 * Realtime (in Layout) and a 20 s poll keep both fresh. Push alerts for new handoffs
 * can be enabled for this device.
 */
import { ArrowLeft, Send } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import { Thread } from "@/practitioner/components/Thread";
import { useHandoff, useHandoffActions, useHandoffs } from "@/practitioner/data";
import { HANDOFF_STATUS, claimLine, handoffName, sortHandoffs } from "@/practitioner/lib/dashboard";
import type { S } from "@/shared/api/client";
import { useMe } from "@/shared/auth/AuthProvider";
import { ConfirmButton } from "@/shared/components/ConfirmButton";
import { PageHeader } from "@/shared/components/PageHeader";
import { PushCard } from "@/shared/components/PushCard";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { firstName, shortWhen, when } from "@/shared/lib/format";
import { TRIGGERS } from "@/shared/lib/labels";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";
import { Label } from "@/shared/ui/label";
import { Textarea } from "@/shared/ui/textarea";

const WIDE = "(min-width: 1024px)";

export function HandoffsPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const list = useHandoffs();
  const sorted = sortHandoffs(list.data ?? []);
  const waiting = sorted.filter((h) => h.status !== "resolved");

  // Desktop: open the first unresolved handoff when none is selected (as the classic page).
  useEffect(() => {
    if (id || !list.data || !window.matchMedia(WIDE).matches) return;
    const first = sortHandoffs(list.data).find((h) => h.status !== "resolved");
    if (first) void navigate(`/handoffs/${first.id}`, { replace: true });
  }, [id, list.data, navigate]);

  return (
    <>
      <PageHeader title="Handoffs" updatedAt={list.dataUpdatedAt} onRefresh={() => void list.refetch()} refreshing={list.isFetching}>
        Patients who asked to talk to a practitioner, sent a red-flag message, or had an alert escalated. Claim one (or reply) to take over the chat from the automatic advisor.
      </PageHeader>
      {list.isPending && <LoadingBlock rows={3} label="Loading handoffs" />}
      {list.isError && <ErrorState message={list.error.message} onRetry={() => void list.refetch()} />}
      {list.isSuccess && (
        <div className="grid gap-4 lg:grid-cols-[20rem_1fr]">
          <section aria-labelledby="handoff-list-title" className={cn("space-y-4", id && "hidden lg:block")}>
            <h2 id="handoff-list-title" className="sr-only">Conversations</h2>
            {waiting.length === 0 && (
              <EmptyState icon={Icons.handoff} title="No patient is waiting.">
                New requests appear here and in the alerts bell. Turn on alerts below to be notified on this device.
              </EmptyState>
            )}
            {sorted.length > 0 && (
              <ul className="space-y-2">
                {sorted.map((h) => (
                  <li key={h.id}>
                    <Link
                      to={`/handoffs/${h.id}`}
                      aria-current={h.id === id ? "page" : undefined}
                      className={cn(
                        "block rounded-2xl border bg-card p-3 hover:border-primary/60",
                        h.id === id && "border-primary ring-2 ring-primary/30",
                        h.status === "resolved" && "opacity-80",
                      )}
                    >
                      <span className="flex items-center justify-between gap-2">
                        <span className="font-semibold">{handoffName(h)}</span>
                        <span className="text-sm text-muted-foreground tabular">{shortWhen(h.last_message_at ?? h.opened_at)}</span>
                      </span>
                      <span className="mt-1.5 flex flex-wrap gap-1.5">
                        <Tag tone={h.trigger === "red_flag" ? "urgent" : "neutral"}>{TRIGGERS[h.trigger] ?? h.trigger}</Tag>
                        <Tag tone={h.status === "open" ? "warn" : h.status === "assigned" ? "good" : "neutral"}>{HANDOFF_STATUS[h.status]}</Tag>
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
            <PushCard purpose="Get a notification on this device when a patient asks for help or writes in a conversation you claimed. On iPhone, add this page to the Home Screen first." />
          </section>

          <div className={cn(!id && "hidden lg:block")}>
            {id ? (
              <ThreadPanel id={id} />
            ) : (
              <div className="grid h-full min-h-48 place-items-center rounded-2xl border border-dashed bg-card p-6 text-center text-muted-foreground">
                Select a conversation to read it and reply.
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function ThreadPanel({ id }: { id: string }) {
  const detail = useHandoff(id);
  const me = useMe(true);
  const actions = useHandoffActions(id);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");

  if (detail.isPending) return <LoadingBlock rows={3} label="Loading the conversation" />;
  if (detail.isError) return <ErrorState message={detail.error.message} onRetry={() => void detail.refetch()} />;
  const h: S["HandoffSummary"] = detail.data.handoff;
  const name = handoffName(h);
  const resolved = h.status === "resolved";
  const busy = actions.claim.isPending || actions.reply.isPending || actions.resolve.isPending;

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const body = text.trim();
    if (!body) return;
    try {
      await actions.reply.mutateAsync(body);
      setText("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <section aria-labelledby="thread-title" className="flex flex-col gap-4 rounded-2xl border bg-card p-4 sm:p-5">
      <div>
        <Button asChild variant="ghost" size="sm" className="-ml-2 mb-1 lg:hidden">
          <Link to="/handoffs">
            <ArrowLeft aria-hidden /> All handoffs
          </Link>
        </Button>
        <div className="flex flex-wrap items-center gap-2">
          <h2 id="thread-title" className="text-xl font-bold">{name}</h2>
          <Tag tone={h.trigger === "red_flag" ? "urgent" : "neutral"}>{TRIGGERS[h.trigger] ?? h.trigger}</Tag>
          <Tag tone={h.status === "open" ? "warn" : h.status === "assigned" ? "good" : "neutral"}>{HANDOFF_STATUS[h.status]}</Tag>
        </div>
        <p className="mt-1 text-sm text-muted-foreground">
          Opened {when(h.opened_at)}
          {h.reason ? `. ${h.reason}` : ""}
        </p>
        <p className="mt-2 font-medium" role="status">{claimLine(h, me.data?.name ?? null)}</p>
        <div className="mt-3 flex flex-wrap gap-2">
          {h.status === "open" && (
            <ConfirmButton
              title={`Claim ${name}'s conversation?`}
              description={<p>The automatic advisor stops answering {name}; their messages come to you until you resolve it.</p>}
              confirmLabel="Claim"
              disabled={busy}
              onConfirm={() =>
                actions.claim.mutate(undefined, {
                  onSuccess: () => toast.success("Claimed: the patient's messages now come to you."),
                  onError: (err) => toast.error(err.message),
                })
              }
            >
              <Icons.handoff aria-hidden /> Claim
            </ConfirmButton>
          )}
          {!resolved && (
            <ConfirmButton
              variant="outline"
              title={`Resolve ${name}'s conversation?`}
              description={<p>The automatic advisor answers {name} again. The conversation is kept for the record.</p>}
              extra={
                <div className="space-y-1.5">
                  <Label htmlFor="resolve-note">Resolution note (optional)</Label>
                  <Textarea id="resolve-note" value={note} onChange={(e) => setNote(e.target.value)} rows={3} maxLength={1000} />
                </div>
              }
              confirmLabel="Resolve"
              disabled={busy}
              onConfirm={() =>
                actions.resolve.mutate(note.trim() || null, {
                  onSuccess: () => {
                    setNote("");
                    toast.success("Handoff resolved.");
                  },
                  onError: (err) => toast.error(err.message),
                })
              }
            >
              Resolve
            </ConfirmButton>
          )}
        </div>
      </div>

      <div className="max-h-[60vh] min-h-40 overflow-y-auto rounded-xl border bg-background/40 p-3">
        <Thread messages={detail.data.messages} patientName={name} />
      </div>

      {resolved ? (
        <p className="text-muted-foreground">Resolved{h.resolved_at ? ` ${when(h.resolved_at)}` : ""}. The automatic advisor answers {firstName(name, "the patient")} again.</p>
      ) : (
        <form onSubmit={(e) => void send(e)} className="space-y-2">
          <Label htmlFor="reply">Reply to {name}</Label>
          <Textarea
            id="reply"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={3}
            maxLength={2000}
            placeholder="Write your reply. Ctrl+Enter sends."
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
            }}
          />
          <div className="flex items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">{h.status === "open" ? "Replying also claims the conversation." : ""}</p>
            <Button type="submit" disabled={busy || !text.trim()}>
              <Send aria-hidden /> {actions.reply.isPending ? "Sending…" : "Send"}
            </Button>
          </div>
        </form>
      )}
    </section>
  );
}
