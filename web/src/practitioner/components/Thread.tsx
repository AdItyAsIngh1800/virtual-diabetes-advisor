/**
 * A handoff conversation as the practitioner sees it, with day separators (local
 * time). Advisor and patient text is shown exactly as stored.
 */
import { useEffect, useRef } from "react";

import type { S } from "@/shared/api/client";
import { Icons } from "@/shared/icons";
import { clock, isDay, withDays } from "@/shared/lib/format";
import { cn } from "@/shared/lib/utils";

const WHO: Record<S["ChatMessageOut"]["role"], string> = {
  patient: "Patient",
  assistant: "Advisor (automatic)",
  practitioner: "You",
  system: "",
};

export function Thread({ messages, patientName, className }: { messages: readonly S["ChatMessageOut"][]; patientName: string; className?: string }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length]);
  if (!messages.length) return <p className="text-muted-foreground">No messages yet.</p>;
  return (
    <div className={cn("space-y-3", className)} role="log" aria-label={`Conversation with ${patientName}`} aria-live="polite">
      {withDays(messages).map((item) => {
        if (isDay(item)) {
          return (
            <div key={item.key} role="separator" className="flex items-center gap-3 py-1 text-sm font-semibold text-muted-foreground">
              <span className="h-px flex-1 bg-border" aria-hidden />
              {item.day}
              <span className="h-px flex-1 bg-border" aria-hidden />
            </div>
          );
        }
        const m = item;
        if (m.role === "system") {
          return (
            <p key={m.id} className="text-center text-sm text-muted-foreground">
              {m.text} · {clock(m.created_at)}
            </p>
          );
        }
        const mine = m.role === "practitioner";
        return (
          <div key={m.id} className={cn("flex", mine ? "justify-end" : "justify-start")}>
            <div
              className={cn(
                "max-w-[85%] rounded-2xl px-4 py-2.5 whitespace-pre-wrap",
                mine && "rounded-br-md bg-bubble-doctor text-bubble-doctor-foreground",
                m.role === "patient" && "rounded-bl-md border bg-card",
                m.role === "assistant" && "rounded-bl-md bg-bubble-advisor",
              )}
            >
              <p className="text-xs font-semibold text-muted-foreground">{m.role === "patient" ? patientName : WHO[m.role]}</p>
              <p>{m.text}</p>
              <p className="mt-1 flex items-center justify-end gap-1 text-xs text-muted-foreground tabular">
                {m.spoken && (
                  <>
                    <Icons.voice className="size-3" aria-hidden /> voice ·
                  </>
                )}
                {clock(m.created_at)}
              </p>
            </div>
          </div>
        );
      })}
      <div ref={end} />
    </div>
  );
}
