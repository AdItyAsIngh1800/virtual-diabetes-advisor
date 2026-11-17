/**
 * Session state for both apps: loads the Supabase client, follows sign-in and
 * sign-out, and exposes the session. GET /api/me (role) is a separate query.
 */
import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { api, type S } from "@/shared/api/client";
import { getSupabase } from "@/shared/auth/supabase";

type AuthState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "signed-out"; sb: SupabaseClient }
  | { status: "signed-in"; sb: SupabaseClient; session: Session };

interface AuthContextValue {
  state: AuthState;
  retry: () => void;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Provides the session to both apps. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const queryClient = useQueryClient();

  useEffect(() => {
    let alive = true;
    let unsubscribe: (() => void) | null = null;
    getSupabase()
      .then(async ({ sb }) => {
        const { data } = await sb.auth.getSession();
        if (!alive) return;
        setState(data.session ? { status: "signed-in", sb, session: data.session } : { status: "signed-out", sb });
        const sub = sb.auth.onAuthStateChange((_event, session) => {
          // Deferred: Supabase warns against awaiting its API inside this callback.
          setTimeout(() => {
            if (!alive) return;
            setState((prev) => {
              const prevId = prev.status === "signed-in" ? prev.session.user.id : null;
              if (prevId !== (session?.user.id ?? null)) queryClient.clear();
              return session ? { status: "signed-in", sb, session } : { status: "signed-out", sb };
            });
          }, 0);
        });
        unsubscribe = () => sub.data.subscription.unsubscribe();
      })
      .catch((err: unknown) => {
        if (alive) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      alive = false;
      unsubscribe?.();
    };
  }, [attempt, queryClient]);

  const retry = useCallback(() => {
    setState({ status: "loading" });
    setAttempt((n) => n + 1);
  }, []);
  const signOut = useCallback(async () => {
    const { sb } = await getSupabase();
    await sb.auth.signOut();
  }, []);

  return <AuthContext.Provider value={{ state, retry, signOut }}>{children}</AuthContext.Provider>;
}

/** The session state; must be inside AuthProvider. */
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}

/** GET /api/me: whether this login is a patient or a practitioner. */
export function useMe(enabled: boolean): UseQueryResult<S["MeOut"]> {
  return useQuery({ queryKey: ["me"], queryFn: () => api<S["MeOut"]>("/api/me"), enabled, staleTime: Infinity });
}
