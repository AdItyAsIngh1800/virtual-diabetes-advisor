/** Patient app entry (/app/): providers, service worker, router. */
import "@/index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { toast } from "sonner";

import { App } from "@/patient/App";
import { AuthProvider } from "@/shared/auth/AuthProvider";
import { registerServiceWorker } from "@/shared/auth/supabase";
import { Toaster } from "@/shared/ui/sonner";
import { TooltipProvider } from "@/shared/ui/tooltip";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: true, staleTime: 15_000 } },
});

registerServiceWorker((message) => toast.error(message));

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <TooltipProvider>
            <BrowserRouter basename="/app">
              <App />
            </BrowserRouter>
            <Toaster position="top-center" />
          </TooltipProvider>
        </AuthProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}
