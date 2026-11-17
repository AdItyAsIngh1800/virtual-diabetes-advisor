/**
 * Supabase client bootstrap (once per page) from the public GET /api/app-config, plus
 * the service worker registration both apps share (/app/sw.js, scope /app/).
 */
import { createClient, type RealtimeChannel, type SupabaseClient } from "@supabase/supabase-js";

import type { components } from "@/shared/api/types";

export type AppConfig = components["schemas"]["AppConfigOut"];

let ready: Promise<{ sb: SupabaseClient; config: AppConfig }> | null = null;

async function boot(): Promise<{ sb: SupabaseClient; config: AppConfig }> {
  let res: Response;
  try {
    res = await fetch("/api/app-config");
  } catch {
    throw new Error("Could not reach the server. Check your connection and try again.");
  }
  if (!res.ok) {
    // 503 carries the reason: missing SUPABASE_ANON_KEY, or the database is unreachable.
    let detail = `The server answered ${res.status}.`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new Error(detail);
  }
  const config = (await res.json()) as AppConfig;
  const sb = createClient(config.supabase_url, config.supabase_anon_key);
  return { sb, config };
}

/** The Supabase client and the app config, loaded once; a failure can be retried. */
export function getSupabase(): Promise<{ sb: SupabaseClient; config: AppConfig }> {
  if (!ready) {
    ready = boot().catch((err: unknown) => {
      ready = null;
      throw err;
    });
  }
  return ready;
}

/** Register the push service worker; problems are reported, never thrown. */
export function registerServiceWorker(onError: (message: string) => void): void {
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker
    .register("/app/sw.js", { scope: "/app/" })
    .catch((err: unknown) => onError(`Service worker failed: ${err instanceof Error ? err.message : String(err)}`));
}

/**
 * Join a private Realtime topic (RLS: only topics named after your own auth uid) and
 * call `handler` on each broadcast `event`. Returns a function that leaves it.
 */
export async function listen(topic: string, event: string, handler: (payload: unknown) => void): Promise<() => void> {
  const { sb } = await getSupabase();
  const { data } = await sb.auth.getSession();
  if (!data.session) return () => undefined;
  await sb.realtime.setAuth(data.session.access_token);
  const channel: RealtimeChannel = sb
    .channel(topic, { config: { private: true } })
    .on("broadcast", { event }, ({ payload }: { payload: unknown }) => handler(payload))
    .subscribe();
  return () => {
    void sb.removeChannel(channel);
  };
}
