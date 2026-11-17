/** Overview (R6): KPI tiles (every rate as "x of y"), the last batch run, delivery. */
import { Link } from "react-router";

import { RowTh, TableCard, Td, Th } from "@/practitioner/components/DataTable";
import { useOverview } from "@/practitioner/data";
import { batchLine, deliveryRows, overviewTiles } from "@/practitioner/lib/dashboard";
import { PageHeader } from "@/shared/components/PageHeader";
import { ErrorState, LoadingBlock } from "@/shared/components/states";
import { pct } from "@/shared/lib/format";
import { cn } from "@/shared/lib/utils";

export function OverviewPage() {
  const ov = useOverview();
  return (
    <>
      <PageHeader title="Your patients now" updatedAt={ov.dataUpdatedAt} onRefresh={() => void ov.refetch()} refreshing={ov.isFetching}>
        Readings count from midnight today; alerts and readings below 54 mg/dL over the last 24 hours, by when the reading was taken. Nothing here changes a reading, a limit or an alert.
      </PageHeader>
      {ov.isPending && <LoadingBlock rows={4} label="Loading the overview" />}
      {ov.isError && <ErrorState message={ov.error.message} onRetry={() => void ov.refetch()} />}
      {ov.isSuccess && (
        <>
          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {overviewTiles(ov.data).map((t) => {
              const body = (
                <>
                  <dt className="text-sm font-medium text-muted-foreground">{t.label}</dt>
                  <dd className="mt-1">
                    <span className="block text-3xl font-bold tracking-tight tabular">{t.value}</span>
                    <span className="text-sm text-muted-foreground">
                      {t.urgent && <span className="font-semibold text-critical">Needs attention · </span>}
                      {t.detail}
                    </span>
                  </dd>
                </>
              );
              const cls = cn("block rounded-2xl border bg-card p-4", t.urgent && "border-l-4 border-l-critical");
              return t.to ? (
                <Link key={t.key} to={t.to} className={cn(cls, "hover:border-primary/60")} aria-label={`${t.label}: ${t.value}. ${t.detail}. Open`}>
                  <div>{body}</div>
                </Link>
              ) : (
                <div key={t.key} className={cls}>{body}</div>
              );
            })}
          </dl>
          <p className="mt-4 text-muted-foreground">{batchLine(ov.data.last_batch_run)}</p>

          <h2 className="mt-8 mb-1 text-lg font-bold">Alert delivery, last 24 hours</h2>
          <p className="mb-3 text-muted-foreground">"Delivered" is sent or opened, out of every alert except suppressed repeats (not sent on purpose).</p>
          <TableCard caption="Alert delivery, last 24 hours" className="max-w-2xl">
            <thead>
              <tr>
                <Th>Status</Th>
                <Th num>Alerts</Th>
                <Th num>Out of</Th>
                <Th num>Rate</Th>
              </tr>
            </thead>
            <tbody>
              {deliveryRows(ov.data.delivery).map(([label, cr]) => (
                <tr key={label}>
                  <RowTh>{label}</RowTh>
                  <Td num>{cr.count}</Td>
                  <Td num>{cr.denominator}</Td>
                  <Td num>{pct(cr.rate)}</Td>
                </tr>
              ))}
            </tbody>
          </TableCard>
        </>
      )}
    </>
  );
}
