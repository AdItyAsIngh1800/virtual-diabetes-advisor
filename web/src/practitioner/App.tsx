/**
 * Practitioner app routes and the sign-in gate. Patients who sign in here are sent to
 * the patient app. Opening /app/practitioner/ goes back to the sidebar section last
 * used (deep links always win).
 */
import { useEffect, useRef, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router";

import { Layout } from "@/practitioner/Layout";
import { SECTIONS, sectionOf } from "@/practitioner/nav";
import { AlertsPage } from "@/practitioner/pages/AlertsPage";
import { AuditPage } from "@/practitioner/pages/AuditPage";
import { HandoffsPage } from "@/practitioner/pages/HandoffsPage";
import { KbPage } from "@/practitioner/pages/KbPage";
import { OverviewPage } from "@/practitioner/pages/OverviewPage";
import { PatientDetailPage } from "@/practitioner/pages/PatientDetailPage";
import { PatientsPage } from "@/practitioner/pages/PatientsPage";
import { ReviewPage } from "@/practitioner/pages/ReviewPage";
import { useAuth, useMe } from "@/shared/auth/AuthProvider";
import { SignIn } from "@/shared/auth/SignIn";
import { ErrorState, LoadingBlock, OfflineBanner } from "@/shared/components/states";
import { load, save } from "@/shared/lib/storage";
import { ThemeToggle } from "@/shared/theme";

const LAST_PATH = "practitioner:last-path";

/**
 * Remembers the sidebar section in use (not a patient, tab or filter), and on a
 * fresh load of "/" returns to it.
 */
function useLastSection() {
  const location = useLocation();
  const navigate = useNavigate();
  const first = useRef(true);
  useEffect(() => {
    const section = sectionOf(location.pathname).to;
    if (first.current) {
      first.current = false;
      const last = load<string>(LAST_PATH, "/");
      const known = SECTIONS.some((s) => s.to === last);
      if (location.pathname === "/" && !location.search && last !== "/" && known) {
        void navigate(last, { replace: true });
        return;
      }
    }
    save(LAST_PATH, section);
  }, [location.pathname, location.search, navigate]);
}

export function App() {
  return (
    <Gate>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<OverviewPage />} />
          <Route path="patients" element={<PatientsPage />} />
          <Route path="patients/:id" element={<PatientDetailPage />} />
          <Route path="patients/:id/:tab" element={<PatientDetailPage />} />
          <Route path="alerts" element={<AlertsPage />} />
          <Route path="handoffs" element={<HandoffsPage />} />
          <Route path="handoffs/:id" element={<HandoffsPage />} />
          <Route path="review" element={<ReviewPage />} />
          <Route path="kb" element={<KbPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Gate>
  );
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-dvh">
      <OfflineBanner />
      <header className="glass sticky top-0 z-20 border-b">
        <div className="mx-auto flex max-w-5xl items-center justify-between px-4 py-2">
          <p className="text-lg font-bold tracking-tight">Practitioner dashboard</p>
          <ThemeToggle />
        </div>
      </header>
      <main className="mx-auto max-w-md px-4 py-10">{children}</main>
    </div>
  );
}

function Gate({ children }: { children: ReactNode }) {
  const { state, retry } = useAuth();
  const signedIn = state.status === "signed-in";
  const me = useMe(signedIn);
  useLastSection();

  useEffect(() => {
    if (me.data?.role === "patient") location.replace("/app/");
  }, [me.data]);

  if (state.status === "loading" || (signedIn && (me.isPending || me.data?.role === "patient"))) {
    return (
      <Frame>
        <LoadingBlock rows={3} label="Opening the dashboard" />
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
        <p className="mt-1 mb-6 text-muted-foreground">
          Your patients' readings, limits and alerts, the patients who asked for help, and the advisor's answers to review. You only see patients assigned to you.
        </p>
        <div className="rounded-2xl border bg-card p-5 shadow-sm">
          <SignIn allowSignUp={false} />
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
