/**
 * Queries and mutations of the practitioner app. Every call is an existing /api route
 * (no new write endpoints); the server checks that each patient is assigned to the
 * practitioner asking.
 */
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type S } from "@/shared/api/client";
import {
  alertsQuery,
  auditQuery,
  patientsQuery,
  reviewQuery,
  type AlertFilters,
  type PatientFilters,
  type ReviewFilter,
  type Verdict,
} from "@/practitioner/lib/dashboard";

// Handoffs refresh every 20 s (as the classic page polled), on top of Realtime.
const HANDOFF_POLL_MS = 20_000;

export function useOverview() {
  return useQuery({ queryKey: ["overview"], queryFn: () => api<S["Overview"]>("/api/dashboard/overview") });
}

/** Every page of the patient list: it is short (assigned patients) and sorted here. */
export function usePatients(filters: PatientFilters) {
  return useQuery({
    queryKey: ["patients", filters],
    queryFn: async () => {
      const rows: S["PatientRow"][] = [];
      let cursor: string | null = null;
      for (let page = 0; page < 20; page += 1) {
        const body: S["PatientPage"] = await api<S["PatientPage"]>(patientsQuery(filters, cursor));
        rows.push(...body.items);
        cursor = body.next_cursor;
        if (!cursor) break;
      }
      return rows;
    },
  });
}

export function usePatient(id: string, days: number) {
  return useQuery({
    queryKey: ["patient", id, days],
    queryFn: () => api<S["PatientDetail"]>(`/api/dashboard/patients/${encodeURIComponent(id)}?days=${days}`),
  });
}

export function useAlerts(filters: AlertFilters) {
  return useInfiniteQuery({
    queryKey: ["alerts", filters],
    queryFn: ({ pageParam }) => api<S["AlertPage"]>(alertsQuery(filters, pageParam)),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
}

/** Recent breaches for the header bell (both directions, newest first). */
export function useRecentBreaches() {
  return useQuery({
    queryKey: ["alerts", "bell"],
    queryFn: async () => {
      const since = new Date(Date.now() - 86_400_000).toISOString();
      const pages = await Promise.all(
        (["breach_high", "breach_low"] as const).map((state) =>
          api<S["AlertPage"]>(`/api/dashboard/alerts?limit=10&state=${state}&since=${encodeURIComponent(since)}`),
        ),
      );
      return pages.flatMap((p) => p.items).sort((a, b) => Date.parse(b.measured_at) - Date.parse(a.measured_at));
    },
  });
}

export function useHandoffs() {
  return useQuery({
    queryKey: ["handoffs"],
    queryFn: () => api<S["HandoffSummary"][]>("/api/practitioner/handoffs"),
    refetchInterval: HANDOFF_POLL_MS,
  });
}

export function useHandoff(id: string | null) {
  return useQuery({
    queryKey: ["handoff", id],
    queryFn: () => api<S["HandoffDetailOut"]>(`/api/practitioner/handoffs/${encodeURIComponent(id ?? "")}`),
    enabled: Boolean(id),
    refetchInterval: HANDOFF_POLL_MS,
  });
}

/** Claim, reply and resolve; each refreshes the list and the thread. */
export function useHandoffActions(id: string) {
  const qc = useQueryClient();
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ["handoffs"] }), qc.invalidateQueries({ queryKey: ["handoff", id] })]);
  const base = `/api/practitioner/handoffs/${encodeURIComponent(id)}`;
  return {
    claim: useMutation({ mutationFn: () => api<S["HandoffSummary"]>(`${base}/claim`, "POST"), onSuccess: refresh }),
    reply: useMutation({ mutationFn: (text: string) => api<S["ChatMessageOut"]>(`${base}/messages`, "POST", { text }), onSuccess: refresh }),
    resolve: useMutation({
      mutationFn: (note: string | null) => api<S["HandoffSummary"]>(`${base}/resolve`, "POST", { note }),
      onSuccess: refresh,
    }),
  };
}

export function useTurns(filter: ReviewFilter) {
  return useInfiniteQuery({
    queryKey: ["turns", filter],
    queryFn: ({ pageParam }) => api<S["TurnPage"]>(reviewQuery(filter, pageParam)),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    // A verdict saved on screen must not jump away under the reader's cursor.
    refetchInterval: false,
  });
}

/** One patient's advisor answers, newest first (the patient page's Conversation tab). */
export function usePatientTurns(patientId: string) {
  return useInfiniteQuery({
    queryKey: ["turns", "patient", patientId],
    queryFn: ({ pageParam }) => api<S["TurnPage"]>(reviewQuery("all", pageParam, 25, patientId)),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useReview(turnId: string) {
  return useMutation({
    mutationFn: ({ verdict, note }: { verdict: Verdict; note: string | null }) =>
      api<S["ReviewRecord"]>(`/api/practitioner/turns/${encodeURIComponent(turnId)}/review`, "POST", { verdict, note }),
  });
}

export function useKb() {
  // Drafts are being edited: no background refresh that could reset a form.
  return useQuery({ queryKey: ["kb"], queryFn: () => api<S["KbListing"]>("/api/practitioner/kb"), refetchInterval: false, refetchOnWindowFocus: false });
}

export function useKbActions() {
  const qc = useQueryClient();
  const refresh = () => Promise.all([qc.invalidateQueries({ queryKey: ["kb"] }), qc.invalidateQueries({ queryKey: ["audit"] })]);
  const path = (id: string) => `/api/practitioner/kb/${encodeURIComponent(id)}`;
  return {
    save: useMutation({ mutationFn: ({ id, draft }: { id: string; draft: S["KbDraftIn"] }) => api<S["KbEntryView"]>(path(id), "PUT", draft) }),
    approve: useMutation({
      // Save first, so what is approved is exactly what is on screen.
      mutationFn: async ({ id, draft }: { id: string; draft: S["KbDraftIn"] }) => {
        await api<S["KbEntryView"]>(path(id), "PUT", draft);
        return api<S["KbEntryView"]>(`${path(id)}/approve`, "POST");
      },
      onSuccess: refresh,
    }),
    reject: useMutation({ mutationFn: (id: string) => api<S["KbEntryView"]>(`${path(id)}/reject`, "POST"), onSuccess: refresh }),
    edit: useMutation({ mutationFn: (id: string) => api<S["KbEntryView"]>(`${path(id)}/edit`, "POST"), onSuccess: refresh }),
  };
}

export function useAudit(since: string, until: string) {
  return useQuery({ queryKey: ["audit", since, until], queryFn: () => api<S["AuditSummary"]>(auditQuery(since, until)) });
}

export function useModelCard() {
  return useQuery({ queryKey: ["model-card"], queryFn: () => api<S["ModelCardOut"]>("/api/practitioner/model-card"), staleTime: 300_000 });
}
