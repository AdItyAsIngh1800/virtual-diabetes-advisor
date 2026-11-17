/** The patient's own limits, readings and alerts (GET /api/me/health). No risk data. */
import { useQuery } from "@tanstack/react-query";

import { api, type S } from "@/shared/api/client";

/** Last 30 days, refreshed every minute while the page is open. */
export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: () => api<S["OwnHealth"]>("/api/me/health?days=30"),
    refetchInterval: 60_000,
  });
}
