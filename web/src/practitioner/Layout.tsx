/**
 * Practitioner chrome: a collapsible frosted sidebar on desktop, a bottom tab bar on
 * phones, and a header with the signed-in practitioner, the alerts bell (waiting
 * handoffs and breaches), the command palette (Cmd/Ctrl+K), the shortcuts dialog
 * ("?"), the theme toggle and sign out. New handoff messages arrive on the private
 * Realtime topic handoffs:<auth uid> and refresh the handoff queries.
 */
import { useQueryClient } from "@tanstack/react-query";
import { Command as CommandIcon, Keyboard, LogOut, MoreHorizontal, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router";

import { useHandoffs, usePatients, useRecentBreaches } from "@/practitioner/data";
import { NO_PATIENT_FILTERS, handoffName } from "@/practitioner/lib/dashboard";
import { SECTIONS, sectionOf } from "@/practitioner/nav";
import { useAuth, useMe } from "@/shared/auth/AuthProvider";
import { listen } from "@/shared/auth/supabase";
import { OfflineBanner } from "@/shared/components/states";
import { Icons } from "@/shared/icons";
import { firstName, shortWhen } from "@/shared/lib/format";
import { bandLabel } from "@/shared/lib/labels";
import { useStored } from "@/shared/lib/storage";
import { cn } from "@/shared/lib/utils";
import { ThemeToggle } from "@/shared/theme";
import { Button } from "@/shared/ui/button";
import { Command, CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/shared/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/shared/ui/dialog";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/shared/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/popover";

/** True when a key press is typed into a field (shortcuts must not fire there). */
function typing(e: KeyboardEvent): boolean {
  const t = e.target as HTMLElement | null;
  if (!t) return false;
  return t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName);
}

export function Layout() {
  const { state, signOut } = useAuth();
  const uid = state.status === "signed-in" ? state.session.user.id : null;
  const me = useMe(true);
  const name = firstName(me.data?.name, "Practitioner");
  const location = useLocation();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [collapsed, setCollapsed] = useStored<boolean>("practitioner:sidebar-collapsed", false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const section = sectionOf(location.pathname);
  const mainRef = useRef<HTMLElement>(null);

  // Realtime: a new handoff or patient message refreshes the handoff queries.
  useEffect(() => {
    if (!uid) return;
    let leave: (() => void) | null = null;
    let alive = true;
    void listen(`handoffs:${uid}`, "handoff", () => {
      void qc.invalidateQueries({ queryKey: ["handoffs"] });
      void qc.invalidateQueries({ queryKey: ["handoff"] });
      void qc.invalidateQueries({ queryKey: ["overview"] });
    }).then((fn) => {
      if (alive) leave = fn;
      else fn();
    });
    return () => {
      alive = false;
      leave?.();
    };
  }, [uid, qc]);

  // Keyboard: Cmd/Ctrl+K palette, "?" shortcuts, "g" then a letter to jump, "/" palette.
  const pendingG = useRef<number | null>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
        return;
      }
      if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "?") {
        e.preventDefault();
        setHelpOpen(true);
      } else if (e.key === "/") {
        e.preventDefault();
        setPaletteOpen(true);
      } else if (e.key === "g") {
        pendingG.current = window.setTimeout(() => (pendingG.current = null), 1200);
      } else if (pendingG.current !== null) {
        window.clearTimeout(pendingG.current);
        pendingG.current = null;
        const target = SECTIONS.find((s) => s.key === e.key.toLowerCase());
        if (target) void navigate(target.to);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  // Move focus to the page on navigation, so screen readers start at the new content.
  useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }, [location.pathname]);

  useEffect(() => {
    document.title = `${section.label} · Practitioner dashboard`;
  }, [section.label]);

  return (
    <div className="flex min-h-dvh">
      <a href="#main" className="sr-only-focusable fixed top-2 left-2 z-50 rounded-lg bg-primary px-4 py-2 text-primary-foreground">Skip to content</a>

      <aside
        aria-label="Sections"
        className={cn("glass sticky top-0 hidden h-dvh shrink-0 flex-col border-r md:flex", collapsed ? "w-[4.5rem]" : "w-60")}
      >
        <div className={cn("flex h-14 items-center border-b px-3", collapsed ? "justify-center" : "justify-between")}>
          {!collapsed && (
            <Link to="/" className="flex items-center gap-2 px-1 font-bold tracking-tight">
              <Icons.reading className="size-5 text-primary" aria-hidden /> Care dashboard
            </Link>
          )}
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? "Expand the sidebar" : "Collapse the sidebar"}
            aria-expanded={!collapsed}
          >
            {collapsed ? <PanelLeftOpen aria-hidden /> : <PanelLeftClose aria-hidden />}
          </Button>
        </div>
        <nav className="flex-1 overflow-y-auto p-2">
          <ul className="space-y-1">
            {SECTIONS.map((s) => (
              <li key={s.to}>
                <NavLink
                  to={s.to}
                  end={s.to === "/"}
                  title={collapsed ? s.label : undefined}
                  aria-label={collapsed ? s.label : undefined}
                  className={({ isActive }) =>
                    cn(
                      "flex min-h-11 items-center gap-3 rounded-xl px-3 font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                      collapsed && "justify-center px-0",
                      isActive && "bg-accent text-accent-foreground",
                    )
                  }
                >
                  <s.icon className="size-5 shrink-0" aria-hidden />
                  {!collapsed && <span>{s.label}</span>}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <div className={cn("border-t p-2", collapsed && "flex justify-center")}>
          <Button variant="ghost" className={cn("w-full justify-start text-muted-foreground", collapsed && "w-auto")} onClick={() => setHelpOpen(true)} aria-label="Keyboard shortcuts">
            <Keyboard aria-hidden />
            {!collapsed && "Shortcuts"}
          </Button>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <OfflineBanner />
        <header className="glass sticky top-0 z-20 border-b">
          <div className="flex h-14 items-center gap-2 px-4">
            <p className="flex-1 truncate font-semibold md:text-lg">
              {section.label}
            </p>
            <Button variant="outline" size="sm" onClick={() => setPaletteOpen(true)} className="hidden text-muted-foreground sm:inline-flex" title="Search patients and sections (Ctrl+K)">
              <CommandIcon aria-hidden /> Search <kbd className="ml-1 rounded border bg-muted px-1.5 text-xs">⌘K</kbd>
            </Button>
            <Button variant="ghost" size="icon" onClick={() => setPaletteOpen(true)} className="sm:hidden" aria-label="Search patients and sections">
              <CommandIcon aria-hidden />
            </Button>
            <AlertsBell />
            <ThemeToggle />
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" className="gap-2 px-2">
                  <span aria-hidden className="grid size-8 place-items-center rounded-full bg-primary font-bold text-primary-foreground">{name.charAt(0)}</span>
                  <span className="sr-only">Account menu: </span>
                  <span className="max-w-32 truncate max-lg:sr-only">{name}</span>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem disabled>Signed in as {name}</DropdownMenuItem>
                <DropdownMenuItem onSelect={() => setHelpOpen(true)}>
                  <Keyboard aria-hidden /> Keyboard shortcuts
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => void signOut()}>
                  <LogOut aria-hidden /> Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </header>

        <main id="main" ref={mainRef} tabIndex={-1} className="mx-auto w-full max-w-6xl flex-1 px-4 pt-6 pb-28 outline-none md:px-8 md:pb-10">
          <Outlet />
        </main>
      </div>

      <PhoneTabBar />
      <Palette open={paletteOpen} onOpenChange={setPaletteOpen} />
      <ShortcutsDialog open={helpOpen} onOpenChange={setHelpOpen} />
    </div>
  );
}

/** Bottom tab bar under 768 px: four sections and a More menu. */
function PhoneTabBar() {
  const location = useLocation();
  const navigate = useNavigate();
  const primary = SECTIONS.filter((s) => s.primary);
  const more = SECTIONS.filter((s) => !s.primary);
  const inMore = more.some((s) => sectionOf(location.pathname).to === s.to);
  const item = "flex min-h-16 w-full flex-col items-center justify-center gap-0.5 text-xs font-semibold text-muted-foreground";
  return (
    <nav aria-label="Sections" className="glass fixed inset-x-0 bottom-0 z-20 border-t pb-[env(safe-area-inset-bottom)] md:hidden">
      <ul className="grid grid-cols-5">
        {primary.map((s) => (
          <li key={s.to}>
            <NavLink to={s.to} end={s.to === "/"} className={({ isActive }) => cn(item, isActive && "text-primary")}>
              {({ isActive }) => (
                <>
                  <span className={cn("grid h-7 w-12 place-items-center rounded-full", isActive && "bg-accent")}>
                    <s.icon className="size-5" aria-hidden />
                  </span>
                  {s.label}
                </>
              )}
            </NavLink>
          </li>
        ))}
        <li>
          <DropdownMenu>
            <DropdownMenuTrigger className={cn(item, inMore && "text-primary")} aria-label="More sections">
              <span className={cn("grid h-7 w-12 place-items-center rounded-full", inMore && "bg-accent")}>
                <MoreHorizontal className="size-5" aria-hidden />
              </span>
              More
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top">
              {more.map((s) => (
                <DropdownMenuItem key={s.to} onSelect={() => void navigate(s.to)} className="min-h-11">
                  <s.icon aria-hidden /> {s.label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </li>
      </ul>
    </nav>
  );
}

/** The bell: waiting handoffs and breaches in the last 24 hours. */
function AlertsBell() {
  const handoffs = useHandoffs();
  const breaches = useRecentBreaches();
  const waiting = (handoffs.data ?? []).filter((h) => h.status === "open");
  const recent = breaches.data ?? [];
  const count = waiting.length + recent.length;
  const [open, setOpen] = useState(false);
  const label = count ? `Alerts: ${waiting.length} waiting handoff${waiting.length === 1 ? "" : "s"}, ${recent.length} breach${recent.length === 1 ? "" : "es"} in 24 hours` : "Alerts: nothing new";
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={label} className="relative">
          <Icons.alert aria-hidden />
          {count > 0 && (
            <span aria-hidden className="absolute -top-0.5 -right-0.5 grid h-5 min-w-5 place-items-center rounded-full bg-critical px-1 text-[0.7rem] font-bold text-white dark:text-[#2a0906]">
              {count > 9 ? "9+" : count}
            </span>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 p-0">
        <div className="border-b px-4 py-3">
          <p className="font-semibold">Needs attention</p>
        </div>
        <div className="max-h-96 overflow-y-auto p-2">
          <p className="px-2 pt-1 pb-1 text-sm font-semibold text-muted-foreground">Waiting handoffs</p>
          {waiting.length === 0 && <p className="px-2 pb-2 text-sm text-muted-foreground">No patient is waiting.</p>}
          {waiting.map((h) => (
            <Link key={h.id} to={`/handoffs/${h.id}`} onClick={() => setOpen(false)} className="flex items-center gap-2 rounded-lg px-2 py-2 hover:bg-accent">
              <Icons.handoff className="size-4 text-warning" aria-hidden />
              <span className="flex-1 font-medium">{handoffName(h)}</span>
              <span className="text-sm text-muted-foreground tabular">{shortWhen(h.last_message_at ?? h.opened_at)}</span>
            </Link>
          ))}
          <p className="px-2 pt-3 pb-1 text-sm font-semibold text-muted-foreground">Breaches, last 24 hours</p>
          {recent.length === 0 && <p className="px-2 pb-2 text-sm text-muted-foreground">No breaches.</p>}
          {recent.slice(0, 8).map((a) => (
            <Link key={a.id} to={`/patients/${a.user_id}`} onClick={() => setOpen(false)} className="flex items-center gap-2 rounded-lg px-2 py-2 hover:bg-accent">
              <Icons.reading className="size-4 text-critical" aria-hidden />
              <span className="flex-1">
                <span className="font-medium">{a.first_name || "Patient"}</span> <span className="text-sm text-muted-foreground">{bandLabel(a.state)}</span>
              </span>
              <span className="text-sm text-muted-foreground tabular">{shortWhen(a.measured_at)}</span>
            </Link>
          ))}
        </div>
        <div className="flex gap-2 border-t p-2">
          <Button asChild variant="ghost" size="sm" className="flex-1">
            <Link to="/handoffs" onClick={() => setOpen(false)}>All handoffs</Link>
          </Button>
          <Button asChild variant="ghost" size="sm" className="flex-1">
            <Link to="/alerts?state=breach_high" onClick={() => setOpen(false)}>All alerts</Link>
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** Cmd/Ctrl+K: jump to any patient (by first name) or section. */
function Palette({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const navigate = useNavigate();
  const patients = usePatients(NO_PATIENT_FILTERS);
  const go = (to: string) => {
    onOpenChange(false);
    void navigate(to);
  };
  const rows = useMemo(() => [...(patients.data ?? [])].sort((a, b) => (a.first_name ?? "").localeCompare(b.first_name ?? "")), [patients.data]);
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="Search" description="Jump to a patient or a section">
      <Command>
        <CommandInput placeholder="Type a patient's first name or a section…" />
        <CommandList>
          <CommandEmpty>Nothing matches. Try a first name or a section such as "Alerts".</CommandEmpty>
          <CommandGroup heading="Patients">
            {rows.map((p) => (
              <CommandItem key={p.user_id} value={`${p.first_name ?? "Patient"} ${p.user_id}`} onSelect={() => go(`/patients/${p.user_id}`)}>
                <Icons.patient aria-hidden />
                {p.first_name || "Patient"}
                <span className="ml-auto text-xs text-muted-foreground">{p.last_reading ? bandLabel(p.last_reading.band_state) : "No readings"}</span>
              </CommandItem>
            ))}
          </CommandGroup>
          <CommandGroup heading="Sections">
            {SECTIONS.map((s) => (
              <CommandItem key={s.to} value={`section ${s.label}`} onSelect={() => go(s.to)}>
                <s.icon aria-hidden /> {s.label}
                <span className="ml-auto text-xs text-muted-foreground">g {s.key}</span>
              </CommandItem>
            ))}
            <CommandItem value="section model card risk" onSelect={() => go("/audit#model-card")}>
              <Icons.audit aria-hidden /> Risk model card
            </CommandItem>
          </CommandGroup>
        </CommandList>
      </Command>
    </CommandDialog>
  );
}

/** "?" opens this list of keyboard shortcuts. */
function ShortcutsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const rows: [string, string][] = [
    ["Ctrl K  or  ⌘ K", "Search patients and sections"],
    ["/", "Search patients and sections"],
    ["?", "Show these shortcuts"],
    ...SECTIONS.map((s): [string, string] => [`g then ${s.key}`, `Go to ${s.label}`]),
    ["Esc", "Close a dialog or menu"],
  ];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>Shortcuts do nothing while you are typing in a field.</DialogDescription>
        </DialogHeader>
        <table className="w-full text-sm">
          <caption className="sr-only">Keyboard shortcuts</caption>
          <tbody>
            {rows.map(([keys, what]) => (
              <tr key={keys + what} className="border-b last:border-0">
                <th scope="row" className="py-2 pr-4 text-left font-normal">
                  <kbd className="rounded border bg-muted px-1.5 py-0.5 font-sans text-xs">{keys}</kbd>
                </th>
                <td className="py-2">{what}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </DialogContent>
    </Dialog>
  );
}

