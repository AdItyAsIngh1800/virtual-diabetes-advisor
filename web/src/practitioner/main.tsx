/** Practitioner app entry (/app/practitioner): providers, service worker, router. */
import "@/index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { toast } from "sonner";

import { App } from "@/practitioner/App";
import { AuthProvider } from "@/shared/auth/AuthProvider";
import { registerServiceWorker } from "@/shared/auth/supabase";
import { Toaster } from "@/shared/ui/sonner";
import { TooltipProvider } from "@/shared/ui/tooltip";

// Auto-refresh: every list re-fetches each minute while the tab is visible (handoffs
// every 20 s, as the classic page polled), and on window focus.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 15_000, refetchInterval: 60_000 } },
});

registerServiceWorker((message) => toast.error(message));

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <TooltipProvider>
            <BrowserRouter basename="/app/practitioner">
              <App />
            </BrowserRouter>
            <Toaster position="bottom-right" />
          </TooltipProvider>
        </AuthProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}
