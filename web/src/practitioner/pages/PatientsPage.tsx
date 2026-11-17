/**
 * Patients (R6): the assigned patients, filterable and sortable (both remembered),
 * by first name. A table on desktop, cards on a phone; each opens the patient page.
 */
import { ArrowDown, ArrowUp, ChevronRight } from "lucide-react";
import { Link } from "react-router";

import { TableCard, Td } from "@/practitioner/components/DataTable";
import { RiskNote } from "@/practitioner/components/RiskNote";
import { usePatients } from "@/practitioner/data";
import {
  NO_PATIENT_FILTERS,
  SORT_KEYS,
  SORT_LABELS,
  patientCells,
  sortPatients,
  type PatientFilters,
  type SortKey,
} from "@/practitioner/lib/dashboard";
import { Field, PageHeader, selectClass } from "@/shared/components/PageHeader";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { BAND_LABELS, bandLabel, bandTone, type BandState } from "@/shared/lib/labels";
import { useStored } from "@/shared/lib/storage";
import { Button } from "@/shared/ui/button";

interface Sort {
  key: SortKey;
  descending: boolean;
}

const isFilters = (v: unknown): v is PatientFilters =>
  typeof v === "object" && v !== null && ["band_state", "breach_7d", "live_handoff"].every((k) => typeof (v as Record<string, unknown>)[k] === "string");
const isSort = (v: unknown): v is Sort =>
  typeof v === "object" && v !== null && SORT_KEYS.includes((v as Sort).key) && typeof (v as Sort).descending === "boolean";

const COLUMNS: [string, SortKey | null, boolean][] = [
  ["Patient", "name", false],
  ["Age", "age", false],
  ["Diabetes", null, false],
  ["Last reading", "last", false],
  ["State", "state", false],
  ["Limits F / PM / Low", null, true],
  ["Alerts 7 d", "alerts", true],
  ["Survey risk", "risk", false],
  ["Handoff", null, false],
];

export function PatientsPage() {
  const [filters, setFilters] = useStored<PatientFilters>("practitioner:patient-filters", NO_PATIENT_FILTERS, isFilters);
  const [sort, setSort] = useStored<Sort>("practitioner:patient-sort", { key: "state", descending: false }, isSort);
  const patients = usePatients(filters);
  const rows = patients.data ? sortPatients(patients.data, sort.key, sort.descending) : [];
  const filtered = Object.values(filters).some(Boolean);
  const set = (k: keyof PatientFilters) => (e: React.ChangeEvent<HTMLSelectElement>) => setFilters({ ...filters, [k]: e.target.value });
  const toggle = (key: SortKey) => setSort({ key, descending: sort.key === key ? !sort.descending : false });

  return (
    <>
      <PageHeader title="Patients" updatedAt={patients.dataUpdatedAt} onRefresh={() => void patients.refetch()} refreshing={patients.isFetching}>
        Shown by first name. Limits are fasting / post-meal / low, in mg/dL. Open a patient for their readings, limits, alerts and conversation.
      </PageHeader>

      <form className="mb-4 flex flex-wrap items-end gap-3" onSubmit={(e) => e.preventDefault()} aria-label="Filter and sort patients">
        <Field label="Last reading" id="pf-band">
          <select id="pf-band" className={selectClass} value={filters.band_state} onChange={set("band_state")}>
            <option value="">Any</option>
            {(["breach_high", "breach_low", "pre_breach_high", "pre_breach_low", "normal"] as BandState[]).map((s) => (
              <option key={s} value={s}>{BAND_LABELS[s]}</option>
            ))}
          </select>
        </Field>
        <Field label="Breach in 7 days" id="pf-breach">
          <select id="pf-breach" className={selectClass} value={filters.breach_7d} onChange={set("breach_7d")}>
            <option value="">Any</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
          </select>
        </Field>
        <Field label="Handoff" id="pf-handoff">
          <select id="pf-handoff" className={selectClass} value={filters.live_handoff} onChange={set("live_handoff")}>
            <option value="">Any</option>
            <option value="true">Waiting or claimed</option>
            <option value="false">None</option>
          </select>
        </Field>
        <Field label="Sort by" id="pf-sort">
          <select id="pf-sort" className={selectClass} value={sort.key} onChange={(e) => setSort({ key: e.target.value as SortKey, descending: false })}>
            {SORT_KEYS.map((k) => (
              <option key={k} value={k}>{SORT_LABELS[k]}</option>
            ))}
          </select>
        </Field>
        {filtered && (
          <Button type="button" variant="ghost" onClick={() => setFilters(NO_PATIENT_FILTERS)}>
            Clear filters
          </Button>
        )}
      </form>

      {patients.isPending && <LoadingBlock rows={5} label="Loading patients" />}
      {patients.isError && <ErrorState message={patients.error.message} onRetry={() => void patients.refetch()} />}
      {patients.isSuccess && rows.length === 0 && (
        <EmptyState
          icon={Icons.patient}
          title={filtered ? "No patients match these filters." : "No patients are assigned to you yet."}
          action={filtered ? <Button variant="outline" onClick={() => setFilters(NO_PATIENT_FILTERS)}>Clear filters</Button> : undefined}
        >
          {!filtered && "An administrator assigns patients to practitioners."}
        </EmptyState>
      )}

      {rows.length > 0 && (
        <>
          {/* Phone: one card per patient. */}
          <ul className="space-y-2 md:hidden" aria-label="Patients">
            {rows.map((r) => {
              const c = patientCells(r);
              const state = r.last_reading?.band_state ?? null;
              return (
                <li key={r.user_id}>
                  <Link to={`/patients/${r.user_id}`} className="flex items-center gap-3 rounded-2xl border bg-card p-4 hover:border-primary/60">
                    <div className="min-w-0 flex-1">
                      <p className="font-semibold">{c.name}</p>
                      <p className="text-sm text-muted-foreground">
                        {c.last}
                        {c.lastWhen && `, ${c.lastWhen}`}
                      </p>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {r.last_reading && <Tag tone={bandTone(state)}>{bandLabel(state)}</Tag>}
                        {r.alerts_7d > 0 && <Tag tone={r.breach_7d ? "urgent" : "warn"}>{c.alerts} alerts</Tag>}
                        {r.live_handoff && <Tag>Handoff: {c.handoff}</Tag>}
                      </div>
                    </div>
                    <ChevronRight className="size-5 text-muted-foreground" aria-hidden />
                  </Link>
                </li>
              );
            })}
          </ul>

          {/* Desktop: the sortable table. */}
          <TableCard caption="Patients" className="hidden md:block">
            <thead>
              <tr>
                {COLUMNS.map(([label, key, num]) => (
                  <th
                    key={label}
                    scope="col"
                    aria-sort={key && sort.key === key ? (sort.descending ? "descending" : "ascending") : undefined}
                    className={`border-b bg-muted/60 px-4 py-2.5 text-sm font-semibold whitespace-nowrap text-muted-foreground ${num ? "text-right" : "text-left"}`}
                  >
                    {key ? (
                      <button type="button" onClick={() => toggle(key)} className="inline-flex items-center gap-1 rounded hover:text-foreground">
                        {label}
                        {sort.key === key && (sort.descending ? <ArrowDown className="size-3.5" aria-hidden /> : <ArrowUp className="size-3.5" aria-hidden />)}
                      </button>
                    ) : (
                      label
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const c = patientCells(r);
                const state = r.last_reading?.band_state ?? null;
                return (
                  <tr key={r.user_id} className="hover:bg-muted/50">
                    <Td>
                      <Link to={`/patients/${r.user_id}`} className="font-semibold text-primary underline-offset-4 hover:underline">
                        {c.name}
                      </Link>
                    </Td>
                    <Td className="whitespace-nowrap">{c.age}</Td>
                    <Td>{c.status}</Td>
                    <Td>
                      <span className="tabular whitespace-nowrap">{c.last}</span>
                      {c.lastWhen && <span className="block text-sm whitespace-nowrap text-muted-foreground">{c.lastWhen}</span>}
                    </Td>
                    <Td>{r.last_reading ? <Tag tone={bandTone(state)}>{bandLabel(state)}</Tag> : "-"}</Td>
                    <Td num>{c.limits}</Td>
                    <Td num>{c.alerts}</Td>
                    <Td>
                      <span title={c.riskModel ? `Model ${c.riskModel}` : undefined}>{c.risk}</span>
                    </Td>
                    <Td>{c.handoff}</Td>
                  </tr>
                );
              })}
            </tbody>
          </TableCard>
          <RiskNote className="mt-3" />
        </>
      )}
    </>
  );
}
