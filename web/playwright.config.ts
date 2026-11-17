/**
 * Accessibility scan (npm run test:a11y): axe on every route of both apps, in light
 * and dark mode, failing on serious or critical issues. Runs against a running
 * server (A11Y_BASE_URL, default http://127.0.0.1:8000) with signed-in sessions
 * from A11Y_PATIENT_TOKEN / A11Y_PATIENT_UID and A11Y_PRACTITIONER_TOKEN /
 * A11Y_PRACTITIONER_UID (a Supabase access token and its user id; see e2e/a11y.spec.ts).
 */
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 2,
  reporter: [["list"]],
  use: {
    baseURL: process.env.A11Y_BASE_URL ?? "http://127.0.0.1:8000",
    ...devices["Desktop Chrome"],
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 900 } } },
    { name: "phone", use: { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true } },
  ],
});
