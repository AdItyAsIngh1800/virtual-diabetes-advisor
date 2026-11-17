/** "My limits" and recent readings, in plain words (no risk score, ever). */
import { Link } from "react-router";

import { useHealth } from "@/patient/useHealth";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LastUpdated, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { dayLabel, clock, mgdl } from "@/shared/lib/format";
import { BAND_TONE, PATIENT_BAND_LABELS, PATIENT_CONTEXT_LABELS } from "@/shared/lib/labels";
import { Button } from "@/shared/ui/button";

/** Readings tab. */
export function ReadingsPage() {
  const health = useHealth();
  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="text-2xl font-bold">Readings</h1>
        <LastUpdated at={health.dataUpdatedAt} />
      </div>
      {health.isPending && <LoadingBlock rows={4} label="Loading your readings" />}
      {health.isError && <ErrorState message={health.error.message} onRetry={() => void health.refetch()} />}
      {health.isSuccess && (
        <>
          <MyLimits limits={health.data.limits} />
          <section aria-labelledby="recent-title" className="space-y-3">
            <h2 id="recent-title" className="text-xl font-bold">Last 30 days</h2>
            {health.data.readings.length === 0 ? (
              <EmptyState
                icon={Icons.reading}
                title="No readings yet"
                action={
                  <Button asChild size="lg">
                    <Link to="/">Send a reading in the chat</Link>
                  </Button>
                }
              >
                Type a reading in the chat, like “fasting 132”, and it shows up here.
              </EmptyState>
            ) : (
              <ul className="divide-y overflow-hidden rounded-2xl border bg-card">
                {[...health.data.readings].reverse().map((r) => (
                  <li key={r.id} className="flex items-center gap-3 px-4 py-3">
                    <Icons.reading className="size-5 shrink-0 text-muted-foreground" aria-hidden />
                    <div className="flex-1">
                      <p className="text-xl font-bold tabular">
                        {mgdl(r.value_mg_dl)} <span className="text-base font-normal text-muted-foreground">mg/dL</span>
                      </p>
                      <p className="text-[0.9rem] text-muted-foreground">
                        {PATIENT_CONTEXT_LABELS[r.context] ?? r.context}, {dayLabel(r.measured_at)} {clock(r.measured_at)}
                      </p>
                    </div>
                    {r.band_state ? <Tag tone={BAND_TONE[r.band_state]}>{PATIENT_BAND_LABELS[r.band_state]}</Tag> : <Tag>No limit yet</Tag>}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/** The current personal limits with a one-line explanation. */
export function MyLimits({ limits }: { limits: { fasting_high: number | null; post_meal_high: number | null; low: number | null } }) {
  const rows = [
    { label: "Fasting, upper", value: limits.fasting_high },
    { label: "After a meal, upper", value: limits.post_meal_high },
    { label: "Lower (any time)", value: limits.low },
  ];
  const none = rows.every((r) => r.value === null);
  return (
    <section aria-labelledby="limits-title" className="rounded-2xl border bg-card p-5 shadow-sm">
      <h2 id="limits-title" className="flex items-center gap-2 text-xl font-bold">
        <Icons.limits className="size-5 text-primary" aria-hidden /> My limits
      </h2>
      <p className="mt-1 text-muted-foreground">
        {none
          ? "Your limits are set after your first readings. Until then, follow your doctor's targets."
          : "Your own glucose limits. You get an alert when a reading comes close to one or goes past it."}
      </p>
      {!none && (
        <dl className="mt-4 grid grid-cols-3 gap-2">
          {rows.map((r) => (
            <div key={r.label} className="rounded-xl bg-muted p-3">
              <dt className="text-[0.85rem] leading-tight text-muted-foreground">{r.label}</dt>
              <dd className="mt-1 text-2xl font-bold tabular">
                {mgdl(r.value)}
                <span className="block text-[0.8rem] font-normal text-muted-foreground">mg/dL</span>
              </dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
