/**
 * Audit (R5): answer counts with their rates (overall and per KB version), the KB
 * status, and the risk model card: precision, recall and F1 before ROC-AUC, the
 * confusion matrix, the share of flagged people without diabetes, the full
 * limitations list and the last retrain. Every rate is shown with its counts.
 */
import { useEffect, useState, type FormEvent } from "react";
import { useLocation } from "react-router";

import { RowTh, TableCard, Td, Th } from "@/practitioner/components/DataTable";
import { useAudit, useModelCard } from "@/practitioner/data";
import { AUDIT_KB_COLUMNS, MODEL_METRICS, auditRows, flaggedLine, kbStatusLine, retrainLine } from "@/practitioner/lib/dashboard";
import type { S } from "@/shared/api/client";
import { Field, PageHeader } from "@/shared/components/PageHeader";
import { ErrorState, LoadingBlock } from "@/shared/components/states";
import { countOf, localDay, pct, when } from "@/shared/lib/format";
import { RISK_LABEL } from "@/shared/lib/labels";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";

export function AuditPage() {
  const [range, setRange] = useState({ since: "", until: "" });
  const audit = useAudit(range.since, range.until);
  const card = useModelCard();
  const location = useLocation();

  // /audit#model-card (from every risk label) scrolls to the model card.
  useEffect(() => {
    if (location.hash !== "#model-card" || !card.data) return;
    const el = document.getElementById("model-card");
    el?.scrollIntoView({ block: "start" });
    el?.focus({ preventScroll: true });
  }, [location.hash, card.data]);

  return (
    <>
      <PageHeader
        title="Audit"
        updatedAt={audit.dataUpdatedAt}
        onRefresh={() => {
          void audit.refetch();
          void card.refetch();
        }}
        refreshing={audit.isFetching}
      >
        The same answers as the Review page: replies the automatic advisor gave your patients. Verdicts are each answer's latest review. Every rate is shown with the counts it comes from.
      </PageHeader>

      {audit.data && <RangeForm key={`${audit.data.since}-${audit.data.until}`} summary={audit.data} onApply={setRange} />}
      {audit.isPending && <LoadingBlock rows={4} label="Loading the audit summary" />}
      {audit.isError && <ErrorState message={audit.error.message} onRetry={() => void audit.refetch()} />}
      {audit.data && <Summary s={audit.data} />}

      <section id="model-card" tabIndex={-1} aria-labelledby="model-card-title" className="mt-10 scroll-mt-20 outline-none">
        <h2 id="model-card-title" className="text-xl font-bold">Risk model card</h2>
        <p className="mt-1 mb-4 max-w-3xl text-muted-foreground">
          The model behind each patient's tier: {RISK_LABEL}. Numbers are from the held-out test set.
        </p>
        {card.isPending && <LoadingBlock rows={3} label="Loading the model card" />}
        {card.isError && <ErrorState message={card.error.message} onRetry={() => void card.refetch()} />}
        {card.data && <ModelCard c={card.data} />}
      </section>
    </>
  );
}

function RangeForm({ summary, onApply }: { summary: S["AuditSummary"]; onApply: (r: { since: string; until: string }) => void }) {
  // until is exclusive: the last day shown is the one before it.
  const [since, setSince] = useState(localDay(summary.since));
  const [until, setUntil] = useState(localDay(summary.until, 1));
  const [problem, setProblem] = useState<string | null>(null);
  function submit(e: FormEvent) {
    e.preventDefault();
    if (since && until && since > until) {
      setProblem("The From date must be on or before the To date.");
      return;
    }
    setProblem(null);
    onApply({ since, until });
  }
  return (
    <form onSubmit={submit} className="mb-4 flex flex-wrap items-end gap-3" aria-label="Audit period">
      <Field label="From" id="audit-since">
        <Input id="audit-since" type="date" value={since} onChange={(e) => setSince(e.target.value)} className="h-10" />
      </Field>
      <Field label="To (included)" id="audit-until">
        <Input id="audit-until" type="date" value={until} onChange={(e) => setUntil(e.target.value)} className="h-10" />
      </Field>
      <Button type="submit">Show period</Button>
      {problem && (
        <p role="alert" className="basis-full font-semibold text-critical">
          {problem}
        </p>
      )}
    </form>
  );
}

function Summary({ s }: { s: S["AuditSummary"] }) {
  const t = s.totals;
  return (
    <div className="space-y-8">
      <p className="text-muted-foreground">
        {when(s.since)} to {when(s.until)}
      </p>
      <TableCard caption="Advisor answers" captionVisible className="max-w-3xl">
        <thead>
          <tr>
            <Th>Answers</Th>
            <Th num>Count</Th>
            <Th num>Out of</Th>
            <Th num>Rate</Th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <RowTh>Advisor answers</RowTh>
            <Td num>{t.total}</Td>
            <Td num />
            <Td num />
          </tr>
          {auditRows(t).map(([label, cr]) => (
            <tr key={label}>
              <RowTh>{label}</RowTh>
              <Td num>{cr.count}</Td>
              <Td num>{cr.denominator}</Td>
              <Td num>{pct(cr.rate)}</Td>
            </tr>
          ))}
        </tbody>
      </TableCard>

      <section aria-labelledby="by-kb-title">
        <h2 id="by-kb-title" className="mb-1 text-lg font-bold">By knowledge-base version</h2>
        <p className="mb-3 text-muted-foreground">Each cell is a count out of that version's answers or reviews, with its rate.</p>
        {s.by_kb_version.length === 0 ? (
          <p className="text-muted-foreground">No answers in this period.</p>
        ) : (
          <TableCard caption="Answers by knowledge-base version">
            <thead>
              <tr>
                <Th>KB version</Th>
                <Th num>Answers</Th>
                {AUDIT_KB_COLUMNS.map(([h]) => (
                  <Th key={h} num>{h}</Th>
                ))}
              </tr>
            </thead>
            <tbody>
              {s.by_kb_version.map((g) => (
                <tr key={String(g.kb_version)}>
                  <RowTh>{g.kb_version === null ? "No KB used" : `v${g.kb_version}`}</RowTh>
                  <Td num>{g.counts.total}</Td>
                  {AUDIT_KB_COLUMNS.map(([h, pick]) => (
                    <Td key={h} num>{countOf(pick(g.counts))}</Td>
                  ))}
                </tr>
              ))}
            </tbody>
          </TableCard>
        )}
      </section>

      <section aria-labelledby="kb-status-title">
        <h2 id="kb-status-title" className="mb-1 text-lg font-bold">Knowledge base</h2>
        <p className="max-w-3xl">{kbStatusLine(s.kb_status)}</p>
      </section>
    </div>
  );
}

function ModelCard({ c }: { c: S["ModelCardOut"] }) {
  const m = c.test_metrics;
  const last = c.recent_retrains[0] ?? null;
  const n = (v: number) => v.toLocaleString();
  return (
    <div className="space-y-6">
      <p className="max-w-3xl">
        Version {c.version} ({c.model_name}), trained {when(c.trained_at)} on {n(c.training_rows)} survey rows; {pct(c.prevalence)} of them had diabetes. Test set: {n(c.test_rows)} rows.
      </p>
      <div className="grid gap-4 lg:grid-cols-2">
        <TableCard caption="Test-set metrics (precision, recall and F1 first)" captionVisible>
          <thead>
            <tr>
              <Th>Metric</Th>
              <Th num>Value</Th>
            </tr>
          </thead>
          <tbody>
            {MODEL_METRICS.map(([key, label]) => (
              <tr key={key}>
                <RowTh>{label}</RowTh>
                <Td num>{m[key].toFixed(3)}</Td>
              </tr>
            ))}
          </tbody>
        </TableCard>
        <div className="space-y-3">
          <TableCard caption="Confusion matrix (test set)" captionVisible>
            <thead>
              <tr>
                <Th />
                <Th num>Not flagged</Th>
                <Th num>Flagged</Th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <RowTh>No diabetes</RowTh>
                <Td num>{n(c.confusion.tn)} <span className="text-muted-foreground">true negative</span></Td>
                <Td num className="bg-warning-surface text-warning">{n(c.confusion.fp)} <span>false positive</span></Td>
              </tr>
              <tr>
                <RowTh>Diabetes</RowTh>
                <Td num className="bg-warning-surface text-warning">{n(c.confusion.fn)} <span>false negative</span></Td>
                <Td num>{n(c.confusion.tp)} <span className="text-muted-foreground">true positive</span></Td>
              </tr>
            </tbody>
          </TableCard>
          <p className="rounded-2xl border-l-4 border-warning bg-warning-surface p-4 font-semibold text-warning">{flaggedLine(c.flagged_without_diabetes)}</p>
          <p className="text-sm text-muted-foreground">
            Flagged at probability {c.decision_threshold.toFixed(3)} or above. Tiers: moderate from {c.tier_cutoffs.moderate.toFixed(3)}, high from {c.tier_cutoffs.high.toFixed(3)}.
          </p>
        </div>
      </div>

      <section aria-labelledby="limits-title" className="max-w-3xl rounded-2xl border bg-card p-5">
        <h3 id="limits-title" className="text-lg font-bold">Limitations</h3>
        <ul className="mt-2 list-disc space-y-1.5 pl-5">
          {c.limitations.map((l) => (
            <li key={l}>{l}</li>
          ))}
        </ul>
      </section>

      <TableCard caption="Most important features" captionVisible className="max-w-xl">
        <thead>
          <tr>
            <Th>Feature</Th>
            <Th num>Importance</Th>
          </tr>
        </thead>
        <tbody>
          {c.top_features.map((f) => (
            <tr key={f.feature}>
              <RowTh>{f.feature}</RowTh>
              <Td num>{f.importance.toFixed(4)}</Td>
            </tr>
          ))}
        </tbody>
      </TableCard>

      <p>
        {retrainLine(last)}
        {last ? ` (${when(last.finished_at ?? last.started_at)})` : ""}
      </p>
    </div>
  );
}
