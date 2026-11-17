/** Email and password sign-in (Supabase Auth), with optional account creation. */
import { useState, type FormEvent } from "react";

import { getSupabase } from "@/shared/auth/supabase";
import { Button } from "@/shared/ui/button";
import { Input } from "@/shared/ui/input";
import { Label } from "@/shared/ui/label";

/** The sign-in form. `allowSignUp` adds "Create account" (patient app only). */
export function SignIn({ allowSignUp = false, signUpRedirect = "/app/" }: { allowSignUp?: boolean; signUpRedirect?: string }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);

  async function run(work: () => Promise<string | null>) {
    setBusy(true);
    setMessage(null);
    try {
      const text = await work();
      if (text) setMessage({ text, error: false });
    } catch (err) {
      setMessage({ text: err instanceof Error ? err.message : String(err), error: true });
    } finally {
      setBusy(false);
    }
  }

  function signIn(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const { sb } = await getSupabase();
      const { error } = await sb.auth.signInWithPassword({ email, password });
      if (error) throw error;
      return null;
    });
  }

  function signUp() {
    void run(async () => {
      const { sb } = await getSupabase();
      const { data, error } = await sb.auth.signUp({
        email,
        password,
        options: { emailRedirectTo: `${location.origin}${signUpRedirect}` },
      });
      if (error) throw error;
      return data.session ? "Account created." : "Check your email to confirm the account, then sign in.";
    });
  }

  return (
    <form onSubmit={signIn} className="space-y-4" aria-describedby={message ? "signin-message" : undefined}>
      <div className="space-y-1.5">
        <Label htmlFor="email">Email</Label>
        <Input id="email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} className="h-12 text-base" />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="password">Password</Label>
        <Input id="password" type="password" autoComplete="current-password" minLength={6} required value={password} onChange={(e) => setPassword(e.target.value)} className="h-12 text-base" />
      </div>
      <div className="flex flex-wrap gap-3">
        <Button type="submit" size="lg" disabled={busy}>Sign in</Button>
        {allowSignUp && (
          <Button type="button" size="lg" variant="outline" disabled={busy || !email || password.length < 6} onClick={signUp}>
            Create account
          </Button>
        )}
      </div>
      {message && (
        <p id="signin-message" role={message.error ? "alert" : "status"} className={message.error ? "font-medium text-critical" : "text-muted-foreground"}>
          {message.text}
        </p>
      )}
    </form>
  );
}
