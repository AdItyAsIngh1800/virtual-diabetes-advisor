/**
 * One patient (R6) as its own page with tabs: Readings (chart and table), Limits
 * ("Why is this the limit?" and the survey risk tier, labelled), Alerts and
 * Conversation (this patient's advisor answers and handoffs together, newest
 * first). The tab is in the URL and the last one used is remembered.
 */
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { RowTh, TableCard, Td, Th } from "@/practitioner/components/DataTable";
import { GlucoseChart } from "@/practitioner/components/GlucoseChart";
import { RiskNote } from "@/practitioner/components/RiskNote";
import { Thread } from "@/practitioner/components/Thread";
import { useHandoff, useHandoffs, usePatient, usePatientTurns } from "@/practitioner/data";
import { HANDOFF_STATUS, VERDICTS, alertCells, conversationTimeline, diabetesLabel, reviewTags, sourceLine, whyLimits } from "@/practitioner/lib/dashboard";
import { Field, PageHeader, selectClass } from "@/shared/components/PageHeader";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import type { S } from "@/shared/api/client";
import { Icons } from "@/shared/icons";
import { firstName, mgdl, shortWhen, signed, UUID_RE, when } from "@/shared/lib/format";
import { CONTEXT_LABELS, TRIGGERS, bandLabel, bandTone } from "@/shared/lib/labels";
import { useStored } from "@/shared/lib/storage";
import { Button } from "@/shared/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";

// [tab, label, short label for phones]
const TABS = [
  ["readings", "Readings", "Readings"],
  ["limits", "Limits", "Limits"],
  ["alerts", "Alerts", "Alerts"],
  ["conversation", "Conversation", "Chat"],
] as const;
type Tab = (typeof TABS)[number][0];
const isTab = (v: unknown): v is Tab => TABS.some(([t]) => t === v);
const DAYS = [7, 14, 30, 90] as const;
const isDays = (v: unknown): v is number => DAYS.includes(v as 7);

export function PatientDetailPage() {
  const { id = "", tab: tabParam } = useParams();
  const navigate = useNavigate();
  const [days, setDays] = useStored<number>("practitioner:patient-days", 30, isDays);
  // Each patient opens on Readings; a deep link (/patients/<id>/limits) opens its tab.
  const tab: Tab = isTab(tabParam) ? tabParam : "readings";
  const valid = UUID_RE.test(id);
  const detail = usePatient(id, days);

  if (!valid) {
    return <ErrorState message="That patient link is not valid." />;
  }

  const select = (t: string) => {
    if (!isTab(t)) return;
    void navigate(`/patients/${id}/${t}`, { replace: true });
  };
  const d = detail.data;

  return (
    <>
      <Button asChild variant="ghost" size="sm" className="mb-2 -ml-2">
        <Link to="/patients">
          <ArrowLeft aria-hidden /> All patients
        </Link>
      </Button>
      <PageHeader
        title={d ? d.first_name || "Patient" : "Patient"}
        updatedAt={detail.dataUpdatedAt}
        onRefresh={() => void detail.refetch()}
        refreshing={detail.isFetching}
        actions={
          <Field label="Period" id="pd-days" className="min-w-28">
            <select id="pd-days" className={selectClass} value={days} onChange={(e) => setDays(Number(e.target.value))}>
              {DAYS.map((n) => (
                <option key={n} value={n}>Last {n} days</option>
              ))}
            </select>
          </Field>
        }
      >
        {d && `${d.age_band ? `Age ${d.age_band}. ` : ""}${diabetesLabel(d.diabetes_status)}. Pre-breach band: within ${Math.round(d.pre_breach_band * 100)} % of a limit.`}
      </PageHeader>

      {detail.isPending && <LoadingBlock rows={4} label="Loading the patient" />}
      {detail.isError && <ErrorState message={detail.error.message} onRetry={() => void detail.refetch()} />}
      {d && (
        <Tabs value={tab} onValueChange={select}>
          {/* Four equal columns: every tab fits at 390 px without sideways scroll. */}
          <TabsList className="mb-4 grid h-auto w-full grid-cols-4 sm:inline-flex sm:w-auto">
            {TABS.map(([t, label, short]) => (
              <TabsTrigger key={t} value={t} className="min-h-10 min-w-0 px-1 sm:px-4">
                <span className="sm:hidden">{short}</span>
                <span className="hidden sm:inline">{label}</span>
              </TabsTrigger>
            ))}
          </TabsList>
          <TabsContent value="readings">
            <ReadingsTab d={d} />
          </TabsContent>
          <TabsContent value="limits">
            <LimitsTab d={d} />
          </TabsContent>
          <TabsContent value="alerts">
            <AlertsTab d={d} />
          </TabsContent>
          <TabsContent value="conversation">
            <ConversationTab d={d} />
          </TabsContent>
        </Tabs>
      )}
    </>
  );
}

function ReadingsTab({ d }: { d: S["PatientDetail"] }) {
  const rows = [...d.readings].reverse();
  return (
    <section aria-labelledby="pd-chart-title" className="space-y-4">
      <h2 id="pd-chart-title" className="text-lg font-bold">Glucose and personal limits</h2>
      <GlucoseChart detail={d} />
      {rows.length === 0 ? (
        <EmptyState icon={Icons.reading} title="No readings in this period.">Choose a longer period above to see older readings.</EmptyState>
      ) : (
        <TableCard caption={`Readings, newest first (${rows.length})`} captionVisible>
          <thead>
            <tr>
              <Th>Taken</Th>
              <Th>Context</Th>
              <Th num>mg/dL</Th>
              <Th>State</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <Td className="tabular whitespace-nowrap">{shortWhen(r.measured_at)}</Td>
                <Td>{CONTEXT_LABELS[r.context] ?? r.context}</Td>
                <Td num>{mgdl(r.value_mg_dl)}</Td>
                <Td>
                  <Tag tone={bandTone(r.band_state)}>{bandLabel(r.band_state)}</Tag>
                  {r.before_first_limit && (
                    <>
                      {" "}
                      <span className="mt-1 block text-sm text-muted-foreground">Judged by the first limit</span>
                    </>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </TableCard>
      )}
    </section>
  );
}

function LimitsTab({ d }: { d: S["PatientDetail"] }) {
  const rows = whyLimits(d.thresholds);
  const line = (label: string, pick: (r: (typeof rows)[number]) => string, total = false) => (
    <tr className={total ? "bg-muted/50 font-bold" : undefined}>
      <RowTh>{label}</RowTh>
      {rows.map((r) => (
        <Td key={r.kind} num className={total ? "font-bold" : undefined}>{pick(r)}</Td>
      ))}
    </tr>
  );
  return (
    <div className="space-y-8">
      <section aria-labelledby="why-title">
        <h2 id="why-title" className="text-lg font-bold">Why is this the limit?</h2>
        <p className="mt-1 mb-3 max-w-3xl text-muted-foreground">
          Each current limit is a cited base plus small, bounded adjustments, then held inside a fixed range. The risk adjustment is always 0: the survey-based risk score never moves a limit.
        </p>
        {rows.length === 0 ? (
          <EmptyState icon={Icons.limits} title="No limits computed yet.">They are computed by the next batch run once the patient's profile is complete.</EmptyState>
        ) : (
          <>
            <TableCard caption="How each current limit is made, in mg/dL" className="max-w-3xl">
              <thead>
                <tr>
                  <Th>mg/dL</Th>
                  {rows.map((r) => (
                    <Th key={r.kind} num>{r.label}</Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {line("Base (cited)", (r) => mgdl(r.base))}
                {line("+ Age / health", (r) => signed(r.demo))}
                {line("+ Survey risk", (r) => signed(r.risk))}
                {line("+ Trend", (r) => signed(r.trend))}
                {line("= Sum", (r) => mgdl(r.sum))}
                {line("Allowed range", (r) => `${mgdl(r.min)}-${mgdl(r.max)}`)}
                {line("Limit", (r) => `${mgdl(r.value)}${r.clamped ? " (held in range)" : ""}`, true)}
              </tbody>
            </TableCard>
            <ul className="mt-3 max-w-3xl space-y-1 text-sm">
              {rows.map((r) => (
                <li key={r.kind}>
                  <strong>{r.label}:</strong> {r.citation}. Last change: {r.reason}, {shortWhen(r.since)}.
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section aria-labelledby="risk-title" className="max-w-3xl rounded-2xl border bg-card p-5">
        <h2 id="risk-title" className="text-lg font-bold">Risk tier</h2>
        <RiskNote prefix="" className="mt-1 mb-3" />
        <p>
          {d.risk
            ? `${d.risk.tier.charAt(0).toUpperCase()}${d.risk.tier.slice(1)} (score ${d.risk.score.toFixed(2)}${d.risk.flagged ? ", flagged" : ""}), model ${d.risk.model_version}, scored ${shortWhen(d.risk.assessed_at)}.`
            : "Not scored yet (the survey profile is incomplete, or the batch has not run)."}
        </p>
      </section>
    </div>
  );
}

function AlertsTab({ d }: { d: S["PatientDetail"] }) {
  if (d.advisories.length === 0) {
    return <EmptyState icon={Icons.alert} title="No alerts for this patient yet." />;
  }
  return (
    <TableCard caption="Recent advisories" captionVisible>
      <thead>
        <tr>
          <Th>Reading taken</Th>
          <Th>Limit</Th>
          <Th>State</Th>
          <Th num>Reading vs limit</Th>
          <Th>Escalated</Th>
          <Th>Delivery</Th>
        </tr>
      </thead>
      <tbody>
        {d.advisories.map((a) => {
          const c = alertCells(a);
          return (
            <tr key={a.id}>
              <Td className="tabular whitespace-nowrap">{c.when}</Td>
              <Td>{c.limit}</Td>
              <Td>
                <Tag tone={c.tone}>{c.state}</Tag>
              </Td>
              <Td num>{c.reading}</Td>
              <Td>{c.escalated}</Td>
              <Td>{c.delivery}</Td>
            </tr>
          );
        })}
      </tbody>
    </TableCard>
  );
}

function ConversationTab({ d }: { d: S["PatientDetail"] }) {
  const handoffs = useHandoffs();
  const turns = usePatientTurns(d.user_id);
  const name = d.first_name || "Patient";
  if (handoffs.isPending || turns.isPending) return <LoadingBlock rows={3} label="Loading the conversation" />;
  if (turns.isError) return <ErrorState message={turns.error.message} onRetry={() => void turns.refetch()} />;
  if (handoffs.isError) return <ErrorState message={handoffs.error.message} onRetry={() => void handoffs.refetch()} />;
  const items = conversationTimeline(
    turns.data.pages.flatMap((p) => p.items),
    handoffs.data.filter((h) => h.patient_id === d.user_id),
  );
  if (items.length === 0) {
    return (
      <EmptyState icon={Icons.chat} title={`${name} has not used the chat yet.`}>
        The advisor's answers and any requests to talk to a practitioner appear here, newest first.
      </EmptyState>
    );
  }
  return (
    <section aria-labelledby="conv-title" className="space-y-3">
      <h2 id="conv-title" className="text-lg font-bold">Advisor answers and handoffs, newest first</h2>
      <ul className="space-y-3">
        {items.map((item) =>
          item.kind === "answer" ? (
            <AnswerItem key={`t-${item.turn.turn_id}`} turn={item.turn} name={name} />
          ) : (
            <HandoffItem key={`h-${item.handoff.id}`} handoff={item.handoff} name={name} />
          ),
        )}
      </ul>
      {turns.hasNextPage && (
        <Button variant="outline" onClick={() => void turns.fetchNextPage()} disabled={turns.isFetchingNextPage}>
          {turns.isFetchingNextPage ? "Loading…" : "Load older answers"}
        </Button>
      )}
    </section>
  );
}

function AnswerItem({ turn, name }: { turn: S["ReviewableTurn"]; name: string }) {
  const r = turn.current_review;
  return (
    <li className="rounded-2xl border bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Icons.chat className="size-4 text-muted-foreground" aria-hidden />
        <span className="font-semibold">Advisor answer</span>
        <span className="text-sm text-muted-foreground">{when(turn.created_at)}</span>
        {reviewTags(turn).map((t) => (
          <Tag key={t.text} tone={t.urgent ? "urgent" : "neutral"}>{t.text}</Tag>
        ))}
      </div>
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground">{turn.spoken ? `${name} said (voice)` : `${name} asked`}</h3>
          <p className="mt-1 rounded-xl border bg-background/40 p-3 whitespace-pre-wrap">{turn.question || "(no text: the Talk to a doctor button)"}</p>
        </div>
        <div>
          <h3 className="text-sm font-semibold text-muted-foreground">Advisor answered</h3>
          <p className="mt-1 rounded-xl bg-bubble-advisor p-3 whitespace-pre-wrap">{turn.answer}</p>
        </div>
      </div>
      {turn.sources.length > 0 && <p className="mt-2 text-sm text-muted-foreground">Source: {turn.sources.map(sourceLine).join("; ")}</p>}
      <p className="mt-2 text-sm">
        {r ? (
          <>
            Current verdict: <strong>{VERDICTS[r.verdict]}</strong> by {firstName(r.reviewer_name, "a practitioner")}
          </>
        ) : (
          "Not reviewed yet."
        )}{" "}
        <Link to="/review" className="font-medium text-primary underline underline-offset-4">
          Review answers
        </Link>
      </p>
    </li>
  );
}

function HandoffItem({ handoff: h, name }: { handoff: S["HandoffSummary"]; name: string }) {
  const [open, setOpen] = useState(false);
  const thread = useHandoff(open ? h.id : null);
  return (
    <li className="rounded-2xl border border-l-4 border-l-primary bg-card p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Icons.handoff className="size-4 text-primary" aria-hidden />
        <span className="font-semibold">Handoff</span>
        <Tag tone={h.trigger === "red_flag" ? "urgent" : "neutral"}>{TRIGGERS[h.trigger] ?? h.trigger}</Tag>
        <Tag tone={h.status === "open" ? "warn" : h.status === "assigned" ? "good" : "neutral"}>{HANDOFF_STATUS[h.status]}</Tag>
        <span className="text-sm text-muted-foreground">opened {shortWhen(h.opened_at)}</span>
      </div>
      {h.reason && <p className="mt-2 text-sm text-muted-foreground">{h.reason}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide messages" : "Show messages"}
        </Button>
        <Button asChild size="sm">
          <Link to={`/handoffs/${h.id}`}>{h.status === "resolved" ? "Open in Handoffs" : "Reply in Handoffs"}</Link>
        </Button>
      </div>
      {open && (
        <div className="mt-3 rounded-xl border bg-background/40 p-3">
          {thread.isPending && <LoadingBlock rows={2} label="Loading messages" />}
          {thread.isError && <ErrorState message={thread.error.message} onRetry={() => void thread.refetch()} />}
          {thread.data && <Thread messages={thread.data.messages} patientName={name} />}
        </div>
      )}
    </li>
  );
}
