/** Turn Web Push alerts on or off for this device, with the iPhone Home Screen hint. */
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Icons } from "@/shared/icons";
import { disableAlerts, enableAlerts, needsHomeScreen, pushEnabled, pushSupported } from "@/shared/lib/push";
import { Button } from "@/shared/ui/button";

/** Push alerts card. `purpose` says what the alerts are for. */
export function PushCard({ purpose }: { purpose: string }) {
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    pushEnabled().then(setOn, () => setOn(false));
  }, []);

  async function run(work: () => Promise<string>) {
    setBusy(true);
    setError(null);
    try {
      toast.success(await work());
      setOn(await pushEnabled());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="push-title" className="rounded-2xl border bg-card p-5">
      <h2 id="push-title" className="flex items-center gap-2 text-lg font-bold">
        <Icons.alert className="size-5 text-primary" aria-hidden /> Alerts on this device
      </h2>
      <p className="mt-1 text-muted-foreground">{purpose}</p>
      <p className="mt-2 font-medium" role="status">
        {on === null ? "Checking…" : on ? "Alerts are on for this device." : "Alerts are off for this device."}
      </p>
      <div className="mt-3 flex flex-wrap gap-3">
        <Button size="lg" disabled={busy || on === true} onClick={() => void run(async () => (await enableAlerts(), "Alerts are on for this device."))}>
          Enable alerts
        </Button>
        <Button
          size="lg"
          variant="outline"
          disabled={busy || on === false}
          onClick={() => void run(async () => ((await disableAlerts()) ? "Alerts are off for this device." : "Alerts were already off on this device."))}
        >
          Turn alerts off
        </Button>
      </div>
      {error && (
        <p role="alert" className="mt-3 font-semibold text-critical">
          {error}
        </p>
      )}
      {(needsHomeScreen() || !pushSupported()) && (
        <p className="mt-3 text-muted-foreground">On iPhone, first use Share → Add to Home Screen, then open the app from the home screen.</p>
      )}
    </section>
  );
}
