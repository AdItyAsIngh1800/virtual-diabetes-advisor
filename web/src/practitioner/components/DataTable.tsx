/**
 * Table helpers: a scrollable table on a solid surface, and numeric cells. On phones
 * long tables scroll sideways inside their card, never the page.
 */
import type { ReactNode } from "react";

import { cn } from "@/shared/lib/utils";

export function TableCard({ caption, children, className, captionVisible = false }: { caption: string; children: ReactNode; className?: string; captionVisible?: boolean }) {
  return (
    <div className={cn("overflow-x-auto rounded-2xl border bg-card", className)} role="region" aria-label={caption} tabIndex={0}>
      <table className="w-full border-collapse text-[0.95rem]">
        <caption className={captionVisible ? "px-4 pt-3 text-left font-semibold" : "sr-only"}>{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ children, num = false, className }: { children?: ReactNode; num?: boolean; className?: string }) {
  return (
    <th scope="col" className={cn("border-b bg-muted/60 px-4 py-2.5 text-left text-sm font-semibold whitespace-nowrap text-muted-foreground", num && "text-right", className)}>
      {children}
    </th>
  );
}

export function Td({ children, num = false, className }: { children?: ReactNode; num?: boolean; className?: string }) {
  return <td className={cn("border-b px-4 py-2.5 align-top", num && "text-right tabular whitespace-nowrap", className)}>{children}</td>;
}

export function RowTh({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <th scope="row" className={cn("border-b px-4 py-2.5 text-left font-medium", className)}>
      {children}
    </th>
  );
}
