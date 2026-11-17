/** A page title with its one-line purpose, "last updated" time and a refresh button. */
import { RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { LastUpdated } from "@/shared/components/states";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";

export function PageHeader({ title, children, updatedAt, onRefresh, refreshing, actions, className }: {
  title: ReactNode;
  children?: ReactNode;
  updatedAt?: number;
  onRefresh?: () => void;
  refreshing?: boolean;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between", className)}>
      <div className="max-w-3xl">
        <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
        {children && <p className="mt-1 text-muted-foreground">{children}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {updatedAt !== undefined && <LastUpdated at={updatedAt} />}
        {onRefresh && (
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={refreshing} aria-label="Refresh this page">
            <RefreshCw aria-hidden className={cn(refreshing && "animate-spin")} /> Refresh
          </Button>
        )}
        {actions}
      </div>
    </div>
  );
}

/** A labelled native select (accessible everywhere, styled like the inputs). */
export function Field({ label, id, children, className }: { label: string; id: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn("flex min-w-36 flex-col gap-1", className)}>
      <label htmlFor={id} className="text-sm font-medium text-muted-foreground">{label}</label>
      {children}
    </div>
  );
}

export const selectClass =
  "h-10 rounded-lg border border-input bg-card px-3 text-[0.95rem] text-foreground focus-visible:outline-3 focus-visible:outline-ring";
