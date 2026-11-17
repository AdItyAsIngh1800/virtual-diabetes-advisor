/** A small tag with words (and a glyph); colour is only a second signal. */
import type { ReactNode } from "react";

import type { Tone } from "@/shared/lib/labels";
import { cn } from "@/shared/lib/utils";

const TONES: Record<Tone, string> = {
  neutral: "bg-secondary text-secondary-foreground",
  good: "bg-good-surface text-good",
  warn: "bg-warning-surface text-warning",
  urgent: "bg-critical-surface text-critical ring-1 ring-critical/40",
};

/** One status tag. */
export function Tag({ tone = "neutral", children, className }: { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-[0.8rem] font-semibold", TONES[tone], className)}>
      {children}
    </span>
  );
}
