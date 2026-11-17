/**
 * axe on every route of the patient and practitioner apps (light and dark), at the
 * project's viewport. Fails on serious or critical violations.
 *
 * Signed-in routes need a session: each role's access token and user id come from
 * the environment and are written into Supabase's localStorage slot before the page
 * loads. Without them only the sign-in pages are scanned.
 */
import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

type Role = "patient" | "practitioner";
const CREDS: Record<Role, { token?: string; uid?: string }> = {
  patient: { token: process.env.A11Y_PATIENT_TOKEN, uid: process.env.A11Y_PATIENT_UID },
  practitioner: { token: process.env.A11Y_PRACTITIONER_TOKEN, uid: process.env.A11Y_PRACTITIONER_UID },
};
const THEMES = ["light", "dark"] as const;

async function projectRef(request: APIRequestContext): Promise<string> {
  const config = (await (await request.get("/api/app-config")).json()) as { supabase_url: string };
  return new URL(config.supabase_url).hostname.split(".")[0] ?? "";
}

async function get<T>(request: APIRequestContext, role: Role, path: string): Promise<T> {
  const res = await request.get(path, { headers: { Authorization: `Bearer ${CREDS[role].token ?? ""}` } });
  expect(res.ok(), `${path} -> ${res.status()}`).toBeTruthy();
  return (await res.json()) as T;
}

async function prepare(page: Page, ref: string, role: Role | null, theme: string) {
  const session = role
    ? JSON.stringify({
        access_token: CREDS[role].token,
        token_type: "bearer",
        expires_in: 3600,
        expires_at: Math.floor(Date.now() / 1000) + 3600,
        refresh_token: "a11y",
        user: { id: CREDS[role].uid, aud: "authenticated", role: "authenticated", email: "a11y@localhost", app_metadata: {}, user_metadata: {}, created_at: "2026-01-01T00:00:00Z" },
      })
    : null;
  await page.addInitScript(
    ([key, value, t]) => {
      localStorage.setItem("da:theme", JSON.stringify(t));
      if (value) localStorage.setItem(key as string, value);
      else localStorage.removeItem(key as string);
    },
    [`sb-${ref}-auth-token`, session, theme] as const,
  );
}

/** Scan one route; its serious/critical violations are added to `found`. */
async function scan(page: Page, path: string, ready: string, found: string[]) {
  await page.goto(path);
  await page.locator(ready).first().waitFor({ state: "visible", timeout: 20_000 });
  await page.waitForLoadState("networkidle").catch(() => undefined);
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  const bad = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  const report = bad.map((v) => `${v.id} (${v.impact}): ${v.help}\n  ${v.nodes.slice(0, 4).map((n) => n.target.join(" ")).join("\n  ")}`);
  if (report.length) found.push(`${path}\n${report.join("\n")}`);
}

for (const theme of THEMES) {
  test.describe(`${theme} mode`, () => {
    test("sign-in pages", async ({ page, request }) => {
      const found: string[] = [];
      await prepare(page, await projectRef(request), null, theme);
      await scan(page, "/app/", "form", found);
      await scan(page, "/app/practitioner/", "form", found);
      expect(found, found.join("\n\n")).toEqual([]);
    });

    test("patient app", async ({ page, request }) => {
      test.skip(!CREDS.patient.token, "A11Y_PATIENT_TOKEN not set");
      await prepare(page, await projectRef(request), "patient", theme);
      const found: string[] = [];
      const health = await get<{ advisories: { id: string }[] }>(request, "patient", "/api/me/health");
      await scan(page, "/app/", "main textarea, main input", found);
      await scan(page, "/app/readings", "main h1, main h2", found);
      await scan(page, "/app/alerts", "main h1, main h2", found);
      await scan(page, "/app/account", "main h1, main h2", found);
      const advisory = health.advisories[0];
      if (advisory) await scan(page, `/app/advisory/${advisory.id}`, "main h1", found);
      expect(found, found.join("\n\n")).toEqual([]);
    });

    test("practitioner app", async ({ page, request }) => {
      test.skip(!CREDS.practitioner.token, "A11Y_PRACTITIONER_TOKEN not set");
      await prepare(page, await projectRef(request), "practitioner", theme);
      const found: string[] = [];
      for (const path of ["/", "/patients", "/alerts", "/alerts?state=breach_high", "/handoffs", "/review", "/kb", "/audit"]) {
        await scan(page, `/app/practitioner${path}`, "main h1", found);
      }
      const patients = await get<{ items: { user_id: string }[] }>(request, "practitioner", "/api/dashboard/patients?limit=1");
      const first = patients.items[0];
      if (first) {
        for (const tab of ["readings", "limits", "alerts", "conversation"]) {
          await scan(page, `/app/practitioner/patients/${first.user_id}/${tab}`, "main [role=tabpanel][data-state=active]", found);
        }
      }
      const handoffs = await get<{ id: string }[]>(request, "practitioner", "/api/practitioner/handoffs");
      const h = handoffs[0];
      if (h) await scan(page, `/app/practitioner/handoffs/${h.id}`, "main #thread-title", found);
      expect(found, found.join("\n\n")).toEqual([]);
    });
  });
}
