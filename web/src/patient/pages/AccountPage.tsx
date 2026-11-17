/** Account: push alerts for this device, the theme, and sign out. */
import { LogOut } from "lucide-react";

import { useAuth } from "@/shared/auth/AuthProvider";
import { PushCard } from "@/shared/components/PushCard";
import { useTheme } from "@/shared/theme";
import { Button } from "@/shared/ui/button";

/** Account tab. */
export function AccountPage() {
  const { state, signOut } = useAuth();
  const { theme, toggle } = useTheme();
  const email = state.status === "signed-in" ? state.session.user.email : null;
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Account</h1>
      <PushCard purpose="Get a notification when a reading needs attention, even with the app closed." />
      <section aria-labelledby="look-title" className="rounded-2xl border bg-card p-5">
        <h2 id="look-title" className="text-lg font-bold">Display</h2>
        <p className="mt-1 text-muted-foreground">{theme === "dark" ? "Dark mode is on." : "Light mode is on."}</p>
        <Button size="lg" variant="outline" className="mt-3" onClick={toggle}>
          Use {theme === "dark" ? "light" : "dark"} mode
        </Button>
      </section>
      <section aria-labelledby="me-title" className="rounded-2xl border bg-card p-5">
        <h2 id="me-title" className="text-lg font-bold">Signed in</h2>
        {email && <p className="mt-1 break-all text-muted-foreground">{email}</p>}
        <Button size="lg" variant="outline" className="mt-3" onClick={() => void signOut()}>
          <LogOut aria-hidden /> Sign out
        </Button>
      </section>
    </div>
  );
}
