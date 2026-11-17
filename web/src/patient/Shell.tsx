/**
 * Patient app chrome: frosted header with the always-visible emergency line, the new
 * advisory banner (Supabase Realtime), and the bottom navigation (Chat, Readings,
 * Alerts, Account).
 */
import { Phone, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, NavLink, Outlet } from "react-router";

import { Icons } from "@/shared/icons";
import { useAuth } from "@/shared/auth/AuthProvider";
import { listen } from "@/shared/auth/supabase";
import { OfflineBanner } from "@/shared/components/states";
import { UUID_RE } from "@/shared/lib/format";
import { cn } from "@/shared/lib/utils";
import { ThemeToggle } from "@/shared/theme";
import { Button } from "@/shared/ui/button";
import { useQueryClient } from "@tanstack/react-query";

const STATE_TITLES: Record<string, string> = {
  pre_breach_high: "Glucose close to your upper limit",
  breach_high: "High glucose reading",
  pre_breach_low: "Glucose close to your lower limit",
  breach_low: "Low glucose reading",
};

/** The emergency line: always visible, on a solid surface, with tap-to-call. */
export function EmergencyLine() {
  return (
    <div className="border-t border-critical/30 bg-critical-surface text-critical">
      <p className="mx-auto flex max-w-xl items-center gap-2 px-4 py-1.5 text-[0.85rem] leading-snug font-medium">
        <Icons.emergency className="size-4 shrink-0" aria-hidden />
        <span className="flex-1">Not for emergencies: for chest pain, trouble breathing, confusion or fainting, call 112.</span>
        <a href="tel:112" className="inline-flex min-h-9 items-center gap-1 rounded-full bg-critical px-3 font-bold text-white dark:text-[#2a0906]">
          <Phone className="size-3.5" aria-hidden /> 112
        </a>
      </p>
    </div>
  );
}

const NAV = [
  { to: "/", label: "Chat", icon: Icons.chat, end: true },
  { to: "/readings", label: "Readings", icon: Icons.reading, end: false },
  { to: "/alerts", label: "Alerts", icon: Icons.alert, end: false },
  { to: "/account", label: "Account", icon: Icons.account, end: false },
] as const;

interface AdvisoryPush {
  advisory_id: string;
  state?: string;
}

function isAdvisoryPush(v: unknown): v is AdvisoryPush {
  return typeof v === "object" && v !== null && typeof (v as AdvisoryPush).advisory_id === "string" && UUID_RE.test((v as AdvisoryPush).advisory_id);
}

/** Layout shared by every signed-in patient page. */
export function Shell() {
  const { state } = useAuth();
  const uid = state.status === "signed-in" ? state.session.user.id : null;
  const [advisory, setAdvisory] = useState<AdvisoryPush | null>(null);
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!uid) return;
    let leave: (() => void) | null = null;
    let alive = true;
    void listen(`advisories:${uid}`, "advisory", (payload) => {
      if (!isAdvisoryPush(payload)) return;
      setAdvisory(payload);
      void queryClient.invalidateQueries({ queryKey: ["health"] });
    }).then((fn) => {
      if (alive) leave = fn;
      else fn();
    });
    return () => {
      alive = false;
      leave?.();
    };
  }, [uid, queryClient]);

  return (
    <div className="flex min-h-dvh flex-col">
      <a href="#main" className="sr-only-focusable fixed top-2 left-2 z-50 rounded-lg bg-primary px-4 py-2 text-primary-foreground">Skip to content</a>
      <OfflineBanner />
      <header className="glass sticky top-0 z-20 border-b">
        <div className="mx-auto flex max-w-xl items-center justify-between px-4 py-2">
          <Link to="/" className="text-lg font-bold tracking-tight">Diabetes Advisor</Link>
          <ThemeToggle />
        </div>
        <EmergencyLine />
      </header>

      {advisory && (
        <section role="alert" className="mx-auto mt-3 w-full max-w-xl px-4">
          <div className="flex items-start gap-3 rounded-2xl border-l-4 border-warning bg-card p-4 shadow-sm">
            <Icons.alert className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden />
            <div className="flex-1">
              <p className="font-bold">{STATE_TITLES[advisory.state ?? ""] ?? "New glucose advice"}</p>
              <p className="text-muted-foreground">A new advisory just arrived for you.</p>
              <Button asChild size="lg" className="mt-3">
                <Link to={`/advisory/${advisory.advisory_id}`} onClick={() => setAdvisory(null)}>Open advice</Link>
              </Button>
            </div>
            <Button variant="ghost" size="icon" aria-label="Dismiss" onClick={() => setAdvisory(null)}>
              <X aria-hidden />
            </Button>
          </div>
        </section>
      )}

      <main id="main" className="mx-auto flex w-full max-w-xl flex-1 flex-col px-4 pt-4 pb-28">
        <Outlet />
      </main>

      <nav aria-label="Main" className="glass fixed inset-x-0 bottom-0 z-20 border-t pb-[env(safe-area-inset-bottom)]">
        <ul className="mx-auto grid max-w-xl grid-cols-4">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <li key={to}>
              <NavLink
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    "flex min-h-16 flex-col items-center justify-center gap-0.5 text-[0.8rem] font-semibold text-muted-foreground",
                    isActive && "text-primary",
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <span className={cn("grid h-8 w-14 place-items-center rounded-full", isActive && "bg-accent")}>
                      <Icon className="size-5" aria-hidden />
                    </span>
                    {label}
                  </>
                )}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  );
}
