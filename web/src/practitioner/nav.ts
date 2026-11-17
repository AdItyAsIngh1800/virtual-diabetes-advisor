/**
 * The practitioner app's sections: one list drives the sidebar, the phone tab bar,
 * the command palette and the "g then letter" shortcuts.
 */
import type { LucideIcon } from "lucide-react";

import { Icons } from "@/shared/icons";

export interface Section {
  to: string;
  label: string;
  icon: LucideIcon;
  /** Second key of the "g then key" shortcut. */
  key: string;
  /** Shown in the phone tab bar (the rest go under More). */
  primary: boolean;
}

const OVERVIEW: Section = { to: "/", label: "Overview", icon: Icons.overview, key: "o", primary: true };

export const SECTIONS: readonly Section[] = [
  OVERVIEW,
  { to: "/patients", label: "Patients", icon: Icons.patient, key: "p", primary: true },
  { to: "/alerts", label: "Alerts", icon: Icons.alert, key: "a", primary: true },
  { to: "/handoffs", label: "Handoffs", icon: Icons.handoff, key: "h", primary: true },
  { to: "/review", label: "Review", icon: Icons.review, key: "r", primary: false },
  { to: "/kb", label: "Knowledge base", icon: Icons.kb, key: "k", primary: false },
  { to: "/audit", label: "Audit", icon: Icons.audit, key: "u", primary: false },
];

/** The section a path belongs to (for the header title and the remembered last tab). */
export function sectionOf(pathname: string): Section {
  const match = SECTIONS.filter((s) => s.to !== "/" && (pathname === s.to || pathname.startsWith(`${s.to}/`)));
  return match[0] ?? OVERVIEW;
}
