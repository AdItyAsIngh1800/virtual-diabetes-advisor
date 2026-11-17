/**
 * Per-viewer conveniences in localStorage (theme, filters, sort, last tab). Storage
 * can be missing or throw (private mode, blocked site data), so every access is
 * wrapped and the app works the same without it.
 */
import { useCallback, useState } from "react";

const PREFIX = "da:";

/** Read a JSON value, or `fallback` when missing, unreadable or of the wrong shape. */
export function load<T>(key: string, fallback: T, valid?: (v: unknown) => v is T): T {
  try {
    const raw = window.localStorage.getItem(PREFIX + key);
    if (raw === null) return fallback;
    const value: unknown = JSON.parse(raw);
    if (valid) return valid(value) ? value : fallback;
    return typeof value === typeof fallback ? (value as T) : fallback;
  } catch {
    return fallback;
  }
}

/** Write a JSON value; failures are ignored (the value just is not remembered). */
export function save(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* storage unavailable: not remembered */
  }
}

/** useState that remembers its value in localStorage under `key`. */
export function useStored<T>(key: string, fallback: T, valid?: (v: unknown) => v is T): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(() => load(key, fallback, valid));
  const set = useCallback(
    (v: T) => {
      setValue(v);
      save(key, v);
    },
    [key],
  );
  return [value, set];
}
