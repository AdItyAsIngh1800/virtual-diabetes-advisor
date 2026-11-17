/** The patient's glucose alerts (advisories), each opening its advisory page. */
import { ChevronRight } from "lucide-react";
import { Link } from "react-router";

import { useHealth } from "@/patient/useHealth";
import { Tag } from "@/shared/components/StateTag";
import { EmptyState, ErrorState, LastUpdated, LoadingBlock } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { clock, dayLabel, mgdl } from "@/shared/lib/format";
import { BAND_TONE, PATIENT_BAND_LABELS, PATIENT_CONTEXT_LABELS } from "@/shared/lib/labels";
import { Button } from "@/shared/ui/button";

/** Alerts tab. */
export function AlertsPage() {
  const health = useHealth();
  const advisories = health.data?.advisories ?? [];
  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between gap-3">
        <h1 className="text-2xl font-bold">Alerts</h1>
        <LastUpdated at={health.dataUpdatedAt} />
      </div>
      <p className="text-muted-foreground">Advice sent when a reading came close to one of your limits or went past it.</p>
      {health.isPending && <LoadingBlock rows={3} label="Loading your alerts" />}
      {health.isError && <ErrorState message={health.error.message} onRetry={() => void health.refetch()} />}
      {health.isSuccess && advisories.length === 0 && (
        <EmptyState
          icon={Icons.alert}
          title="No alerts"
          action={
            <Button asChild variant="outline" size="lg">
              <Link to="/account">Turn on alerts for this phone</Link>
            </Button>
          }
        >
          When a reading needs attention, the advice appears here and on your phone.
        </EmptyState>
      )}
      {advisories.length > 0 && (
        <ul className="space-y-2">
          {advisories.map((a) => (
            <li key={a.id}>
              <Link to={`/advisory/${a.id}`} className="flex items-center gap-3 rounded-2xl border bg-card p-4 hover:bg-accent">
                <Icons.alert className="size-5 shrink-0 text-muted-foreground" aria-hidden />
                <div className="flex-1">
                  <Tag tone={BAND_TONE[a.state]}>{PATIENT_BAND_LABELS[a.state]}</Tag>
                  <p className="mt-1 font-semibold tabular">
                    {mgdl(a.reading_value_mg_dl)} mg/dL, your limit {mgdl(a.threshold_mg_dl)}
                  </p>
                  <p className="text-[0.9rem] text-muted-foreground">
                    {dayLabel(a.measured_at)} {clock(a.measured_at)}
                    {a.limit_kind === "post_meal_high" ? ` · ${PATIENT_CONTEXT_LABELS.post_meal}` : a.limit_kind === "fasting_high" ? ` · ${PATIENT_CONTEXT_LABELS.fasting}` : ""}
                  </p>
                </div>
                <ChevronRight className="size-5 text-muted-foreground" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
