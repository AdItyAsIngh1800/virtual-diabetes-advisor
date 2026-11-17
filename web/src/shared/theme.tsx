/**
 * Light and dark mode. The reader's choice (header toggle) wins; without one the app
 * follows prefers-color-scheme, and is dark when the system states no preference.
 * The same rule runs inline in each HTML shell before first paint (no flash).
 */
import { Moon, Sun } from "lucide-react";
import { useSyncExternalStore } from "react";

import { load, save } from "@/shared/lib/storage";
import { Button } from "@/shared/ui/button";

export type Theme = "light" | "dark";

const isTheme = (v: unknown): v is Theme => v === "light" || v === "dark";

/** The theme to show: saved choice, else the system's, else dark. */
export function initialTheme(): Theme {
  const saved = load<Theme | null>("theme", null, (v): v is Theme | null => v === null || isTheme(v));
  if (saved) return saved;
  try {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function apply(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  const meta = document.querySelector('meta[name="theme-color"]');
  meta?.setAttribute("content", theme === "dark" ? "#0b1513" : "#eef4f2");
}

let current: Theme | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function snapshot(): Theme {
  if (current === null) {
    current = initialTheme();
    apply(current);
  }
  return current;
}

/** Switch theme and remember the choice. */
export function toggleTheme(): void {
  current = snapshot() === "dark" ? "light" : "dark";
  save("theme", current);
  apply(current);
  listeners.forEach((l) => l());
}

/** Current theme (one store for the whole page) and a toggle. */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const theme = useSyncExternalStore(subscribe, snapshot, () => "dark" as Theme);
  return { theme, toggle: toggleTheme };
}

/** Header button switching light and dark mode. */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, toggle } = useTheme();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <Button variant="ghost" size="icon" onClick={toggle} aria-label={`Switch to ${next} mode`} title={`Switch to ${next} mode`} className={className}>
      {theme === "dark" ? <Sun aria-hidden /> : <Moon aria-hidden />}
    </Button>
  );
}
