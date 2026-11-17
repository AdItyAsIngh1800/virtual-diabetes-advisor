/**
 * Alerts (R6): advisories raised for the practitioner's patients, newest reading
 * first, with filters kept in the URL (deep links such as ?state=breach_high) and
 * remembered for the next visit, and "Load more" paging.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { TableCard, Td, Th } from "@/practitioner/components/DataTable";
import { useAlerts } from "@/practitioner/data";
import { ALERT_FILTER_KEYS, alertCells, type AlertFilters } from "@/practitioner/lib/dashboard";
import { Field, PageHeader, selectClass } from "@/shared/components/PageHeader";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { BAND_LABELS, DELIVERY_LABELS } from "@/shared/lib/labels";
import { load, save } from "@/shared/lib/storage";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";

const EMPTY: AlertFilters = { state: "", escalated: "", delivery_status: "", since: "", until: "" };
const SAVED = "practitioner:alert-filters";
const STATES = ["breach_high", "breach_low", "pre_breach_high", "pre_breach_low"] as const;
const DELIVERY = ["queued", "sent", "opened", "failed", "expired", "suppressed"] as const;

function fromParams(p: URLSearchParams): AlertFilters {
  const f = { ...EMPTY };
  for (const k of ALERT_FILTER_KEYS) f[k] = p.get(k) ?? "";
  return f;
}

export function AlertsPage() {
  const [params, setParams] = useSearchParams();
  const filters = fromParams(params);
  const restored = useRef(false);

  // No filters in the link: restore the last ones used. A deep link always wins.
  useEffect(() => {
    if (restored.current) return;
    restored.current = true;
    if (params.toString() === "") {
      const saved = load<AlertFilters>(SAVED, EMPTY, (v): v is AlertFilters => typeof v === "object" && v !== null);
      if (Object.values(saved).some(Boolean)) apply(saved);
    }
  });

  function apply(f: AlertFilters) {
    const next = new URLSearchParams();
    for (const k of ALERT_FILTER_KEYS) if (f[k]) next.set(k, f[k]);
    setParams(next, { replace: true });
    save(SAVED, f);
  }

  const alerts = useAlerts(filters);
  const rows = alerts.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Object.values(filters).some(Boolean);

  return (
    <>
      <PageHeader title="Alerts" updatedAt={alerts.dataUpdatedAt} onRefresh={() => void alerts.refetch()} refreshing={alerts.isFetching}>
        Advisories raised for your patients, newest reading first. The limit is the one the batch judged the reading against.
      </PageHeader>

      {/* Keyed by the URL: a new deep link resets the form to it. */}
      <FilterForm key={params.toString()} initial={filters} filtered={filtered} onApply={apply} />

      {alerts.isPending && <LoadingBlock rows={5} label="Loading alerts" />}
      {alerts.isError && <ErrorState message={alerts.error.message} onRetry={() => void alerts.refetch()} />}
      {alerts.isSuccess && rows.length === 0 && (
        <EmptyState
          icon={Icons.alert}
          title={filtered ? "No alerts match these filters." : "No alerts yet."}
          action={filtered ? <Button variant="outline" onClick={() => apply(EMPTY)}>Clear filters</Button> : undefined}
        >
          {!filtered && "Alerts appear when a reading comes close to or crosses a patient's personal limit."}
        </EmptyState>
      )}
      {rows.length > 0 && (
        <TableCard caption="Alerts">
          <thead>
            <tr>
              <Th>Reading taken</Th>
              <Th>Patient</Th>
              <Th>Limit</Th>
              <Th>State</Th>
              <Th num>Reading vs limit</Th>
              <Th>Escalated</Th>
              <Th>Delivery</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => {
              const c = alertCells(a);
              return (
                <tr key={a.id} className="hover:bg-muted/50">
                  <Td className="tabular whitespace-nowrap">{c.when}</Td>
                  <Td>
                    <Link to={`/patients/${a.user_id}/alerts`} className="font-semibold text-primary underline-offset-4 hover:underline">
                      {c.name}
                    </Link>
                  </Td>
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
      )}
      {alerts.hasNextPage && (
        <Button variant="outline" className="mt-4" onClick={() => void alerts.fetchNextPage()} disabled={alerts.isFetchingNextPage}>
          {alerts.isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      )}
    </>
  );
}

/** The filter form; it edits a draft and applies it on submit. */
function FilterForm({ initial, filtered, onApply }: { initial: AlertFilters; filtered: boolean; onApply: (f: AlertFilters) => void }) {
  const [draft, setDraft] = useState<AlertFilters>(initial);
  const [problem, setProblem] = useState<string | null>(null);
  const set = (k: keyof AlertFilters) => (e: React.ChangeEvent<HTMLSelectElement | HTMLInputElement>) => setDraft({ ...draft, [k]: e.target.value });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (draft.since && draft.until && draft.since > draft.until) {
      setProblem("The From date must be on or before the To date.");
      return;
    }
    setProblem(null);
    onApply(draft);
  }
  return (
    <>
      <form className="mb-4 flex flex-wrap items-end gap-3" onSubmit={submit} aria-label="Filter alerts">
        <Field label="State" id="af-state">
          <select id="af-state" className={selectClass} value={draft.state} onChange={set("state")}>
            <option value="">Any</option>
            {STATES.map((s) => (
              <option key={s} value={s}>{BAND_LABELS[s]}</option>
            ))}
          </select>
        </Field>
        <Field label="Escalated" id="af-escalated">
          <select id="af-escalated" className={selectClass} value={draft.escalated} onChange={set("escalated")}>
            <option value="">Any</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </Field>
        <Field label="Delivery" id="af-delivery">
          <select id="af-delivery" className={selectClass} value={draft.delivery_status} onChange={set("delivery_status")}>
            <option value="">Any</option>
            {DELIVERY.map((s) => (
              <option key={s} value={s}>{DELIVERY_LABELS[s]}</option>
            ))}
          </select>
        </Field>
        <Field label="From" id="af-since">
          <Input id="af-since" type="date" value={draft.since} onChange={set("since")} className="h-10" />
        </Field>
        <Field label="To (included)" id="af-until">
          <Input id="af-until" type="date" value={draft.until} onChange={set("until")} className="h-10" />
        </Field>
        <Button type="submit">Show alerts</Button>
        {filtered && (
          <Button type="button" variant="ghost" onClick={() => onApply(EMPTY)}>
            Clear filters
          </Button>
        )}
      </form>
      {problem && (
        <p role="alert" className="mb-4 font-semibold text-critical">
          {problem}
        </p>
      )}

    </>
  );
}
