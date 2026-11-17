/**
 * Patient app routes and the sign-in gate. Practitioners who sign in here are sent
 * to /app/practitioner. Old notification links (/app/?advisory=<id>) go to the
 * advisory page.
 */
import { useEffect } from "react";
import { Navigate, Route, Routes, useSearchParams } from "react-router";

import { AccountPage } from "@/patient/pages/AccountPage";
import { AdvisoryPage } from "@/patient/pages/AdvisoryPage";
import { AlertsPage } from "@/patient/pages/AlertsPage";
import { ChatPage } from "@/patient/pages/ChatPage";
import { ReadingsPage } from "@/patient/pages/ReadingsPage";
import { EmergencyLine, Shell } from "@/patient/Shell";
import { useAuth, useMe } from "@/shared/auth/AuthProvider";
import { SignIn } from "@/shared/auth/SignIn";
import { ErrorState, LoadingBlock, OfflineBanner } from "@/shared/components/states";
import { UUID_RE } from "@/shared/lib/format";
import { ThemeToggle } from "@/shared/theme";

/** Routes behind the gate. */
export function App() {
  const [params] = useSearchParams();
  const legacyAdvisory = params.get("advisory");
  if (legacyAdvisory && UUID_RE.test(legacyAdvisory)) return <Navigate to={`/advisory/${legacyAdvisory}`} replace />;
  return (
    <Gate>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<ChatPage />} />
          <Route path="readings" element={<ReadingsPage />} />
          <Route path="alerts" element={<AlertsPage />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="advisory/:id" element={<AdvisoryPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Gate>
  );
}

function Frame({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-dvh">
      <OfflineBanner />
      <header className="glass sticky top-0 z-20 border-b">
        <div className="mx-auto flex max-w-xl items-center justify-between px-4 py-2">
          <p className="text-lg font-bold tracking-tight">Diabetes Advisor</p>
          <ThemeToggle />
        </div>
        <EmergencyLine />
      </header>
      <main className="mx-auto max-w-xl px-4 py-6">{children}</main>
    </div>
  );
}

function Gate({ children }: { children: React.ReactNode }) {
  const { state, retry } = useAuth();
  const signedIn = state.status === "signed-in";
  const me = useMe(signedIn);

  useEffect(() => {
    if (me.data?.role === "practitioner") location.replace("/app/practitioner");
  }, [me.data]);

  if (state.status === "loading" || (signedIn && (me.isPending || me.data?.role === "practitioner"))) {
    return (
      <Frame>
        <LoadingBlock rows={4} label="Opening your advisor" />
      </Frame>
    );
  }
  if (state.status === "error") {
    return (
      <Frame>
        <ErrorState message={state.message} onRetry={retry} />
      </Frame>
    );
  }
  if (state.status === "signed-out") {
    return (
      <Frame>
        <h1 className="text-2xl font-bold">Sign in</h1>
        <p className="mt-1 mb-6 text-muted-foreground">Glucose alerts and help from your care team, on this phone.</p>
        <div className="rounded-2xl border bg-card p-5 shadow-sm">
          <SignIn allowSignUp />
        </div>
      </Frame>
    );
  }
  if (me.isError) {
    return (
      <Frame>
        <ErrorState message={me.error.message} onRetry={() => void me.refetch()} />
      </Frame>
    );
  }
  return <>{children}</>;
}
