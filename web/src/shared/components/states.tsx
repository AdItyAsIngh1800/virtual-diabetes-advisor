/**
 * Loading, error, empty and offline states, and the "last updated" line, shared by
 * every page of both apps.
 */
import { CloudOff, RefreshCw, TriangleAlert, type LucideIcon } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/button";
import { Skeleton } from "@/shared/ui/skeleton";

/** Placeholder blocks while a page loads; announced once to screen readers. */
export function LoadingBlock({ rows = 3, className, label = "Loading" }: { rows?: number; className?: string; label?: string }) {
  return (
    <div className={cn("space-y-3", className)} role="status" aria-live="polite">
      <span className="sr-only">{label}…</span>
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className={cn("h-14 w-full rounded-xl", i === 0 && "h-20")} />
      ))}
    </div>
  );
}

/** What went wrong, and a retry button. */
export function ErrorState({ message, onRetry, className }: { message: string; onRetry?: () => void; className?: string }) {
  return (
    <div role="alert" className={cn("flex flex-col gap-3 rounded-2xl border border-critical/40 bg-critical-surface p-4 text-critical sm:flex-row sm:items-center", className)}>
      <TriangleAlert className="size-5 shrink-0" aria-hidden />
      <p className="flex-1 font-medium">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry} className="self-start text-foreground sm:self-auto">
          <RefreshCw aria-hidden /> Try again
        </Button>
      )}
    </div>
  );
}

/** An empty list that says what to do next. */
export function EmptyState({ icon: Icon, title, children, action, className }: {
  icon: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-start gap-2 rounded-2xl border border-dashed bg-card p-5", className)}>
      <Icon className="size-6 text-muted-foreground" aria-hidden />
      <p className="font-semibold">{title}</p>
      {children && <div className="text-muted-foreground">{children}</div>}
      {action}
    </div>
  );
}

/** True while the browser reports a network connection. */
export function useOnline(): boolean {
  const [online, setOnline] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine));
  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);
  return online;
}

/** A banner while offline: what still works and what does not. */
export function OfflineBanner() {
  const online = useOnline();
  if (online) return null;
  return (
    <div role="status" className="flex items-center gap-2 bg-warning-surface px-4 py-2 text-sm font-medium text-warning">
      <CloudOff className="size-4 shrink-0" aria-hidden />
      You are offline. What is on screen may be out of date; new messages send when you are back online.
    </div>
  );
}

/** "Updated 14:05" for a query's dataUpdatedAt; re-renders each minute. */
export function LastUpdated({ at, className }: { at: number; className?: string }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 60_000);
    return () => clearInterval(t);
  }, []);
  if (!at) return null;
  const time = new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return <span className={cn("text-sm text-muted-foreground tabular", className)}>Updated {time}</span>;
}
