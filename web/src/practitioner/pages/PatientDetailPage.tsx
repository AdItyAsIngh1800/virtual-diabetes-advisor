/**
 * One patient (R6) as its own page with tabs: Readings (chart and table), Limits
 * ("Why is this the limit?" and the survey risk tier, labelled), Alerts and
 * Conversation (this patient's handoff threads). The tab is in the URL and the last
 * one used is remembered.
 */
import { ArrowLeft } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router";

import { RowTh, TableCard, Td, Th } from "@/practitioner/components/DataTable";
import { GlucoseChart } from "@/practitioner/components/GlucoseChart";
import { RiskNote } from "@/practitioner/components/RiskNote";
import { Thread } from "@/practitioner/components/Thread";
import { useHandoff, useHandoffs, usePatient } from "@/practitioner/data";
import { HANDOFF_STATUS, alertCells, diabetesLabel, sortHandoffs, whyLimits } from "@/practitioner/lib/dashboard";
import { Field, PageHeader, selectClass } from "@/shared/components/PageHeader";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import type { S } from "@/shared/api/client";
import { Icons } from "@/shared/icons";
import { mgdl, shortWhen, signed, UUID_RE } from "@/shared/lib/format";
import { CONTEXT_LABELS, TRIGGERS, bandLabel, bandTone } from "@/shared/lib/labels";
import { useStored } from "@/shared/lib/storage";
import { Button } from "@/shared/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/tabs";

const TABS = [
  ["readings", "Readings"],
  ["limits", "Limits"],
  ["alerts", "Alerts"],
  ["conversation", "Conversation"],
] as const;
type Tab = (typeof TABS)[number][0];
const isTab = (v: unknown): v is Tab => TABS.some(([t]) => t === v);
const DAYS = [7, 14, 30, 90] as const;
const isDays = (v: unknown): v is number => DAYS.includes(v as 7);

export function PatientDetailPage() {
  const { id = "", tab: tabParam } = useParams();
  const navigate = useNavigate();
  const [lastTab, setLastTab] = useStored<Tab>("practitioner:patient-tab", "readings", isTab);
  const [days, setDays] = useStored<number>("practitioner:patient-days", 30, isDays);
  const tab: Tab = isTab(tabParam) ? tabParam : lastTab;
  const valid = UUID_RE.test(id);
  const detail = usePatient(id, days);

  if (!valid) {
    return <ErrorState message="That patient link is not valid." />;
  }

  const select = (t: string) => {
    if (!isTab(t)) return;
    setLastTab(t);
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
          <TabsList className="mb-4 w-full justify-start overflow-x-auto sm:w-auto">
            {TABS.map(([t, label]) => (
              <TabsTrigger key={t} value={t} className="min-h-10 px-4">
                {label}
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
                  {r.before_first_limit && <span className="ml-2 text-sm text-muted-foreground">judged by the first limit</span>}
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
  const mine = sortHandoffs((handoffs.data ?? []).filter((h) => h.patient_id === d.user_id));
  const latest = mine[0] ?? null;
  const thread = useHandoff(latest?.id ?? null);
  const name = d.first_name || "Patient";
  if (handoffs.isPending) return <LoadingBlock rows={2} label="Loading the conversation" />;
  if (handoffs.isError) return <ErrorState message={handoffs.error.message} onRetry={() => void handoffs.refetch()} />;
  if (!latest) {
    return (
      <EmptyState icon={Icons.handoff} title={`${name} has not asked to talk to a practitioner.`}>
        Conversations appear here when the patient asks for a practitioner, sends a red-flag message, or an alert is escalated. The advisor's answers are in Review.
      </EmptyState>
    );
  }
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Tag tone={latest.trigger === "red_flag" ? "urgent" : "neutral"}>{TRIGGERS[latest.trigger] ?? latest.trigger}</Tag>
        <Tag>{HANDOFF_STATUS[latest.status]}</Tag>
        <span className="text-sm text-muted-foreground">opened {shortWhen(latest.opened_at)}</span>
        <Button asChild size="sm" className="ml-auto">
          <Link to={`/handoffs/${latest.id}`}>
            <Icons.handoff aria-hidden /> {latest.status === "resolved" ? "Open in Handoffs" : "Reply in Handoffs"}
          </Link>
        </Button>
      </div>
      <div className="rounded-2xl border bg-card p-4">
        {thread.isPending && <LoadingBlock rows={2} label="Loading messages" />}
        {thread.isError && <ErrorState message={thread.error.message} onRetry={() => void thread.refetch()} />}
        {thread.data && <Thread messages={thread.data.messages} patientName={name} />}
      </div>
      {mine.length > 1 && <p className="text-sm text-muted-foreground">{mine.length - 1} earlier handoff{mine.length > 2 ? "s" : ""} in the Handoffs list.</p>}
    </section>
  );
}
