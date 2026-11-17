/// <reference types="vitest/config" />
// One Vite project, two entry points: the patient app (/app/) and the practitioner
// app (/app/practitioner/). The build goes to app/static/web, which FastAPI serves
// with an SPA fallback (app/api/patient_app.py); the built files are committed so the
// demo machine needs no Node.
import path from "node:path";
import type { Connect, Plugin } from "vite";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const OUT_DIR = path.resolve(import.meta.dirname, "../app/static/web");

/** Mirrors FastAPI's SPA fallback for `vite dev` and `vite preview`. */
function spaFallback(): Plugin {
  const rewrite: Connect.NextHandleFunction = (req, _res, next) => {
    const url = req.url ?? "";
    const pathOnly = url.split("?")[0];
    const isFile = /\.[a-z0-9]+$/i.test(pathOnly);
    if (!isFile && (pathOnly === "/app/practitioner" || pathOnly.startsWith("/app/practitioner/"))) {
      req.url = "/app/practitioner.html";
    } else if (!isFile && (pathOnly === "/app" || pathOnly.startsWith("/app/"))) {
      req.url = "/app/patient.html";
    }
    next();
  };
  return {
    name: "spa-fallback",
    configureServer: (server) => { server.middlewares.use(rewrite); },
    configurePreviewServer: (server) => { server.middlewares.use(rewrite); },
  };
}

export default defineConfig({
  base: "/app/",
  plugins: [react(), tailwindcss(), spaFallback()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  preview: { proxy: { "/api": "http://127.0.0.1:8000" } },
  build: {
    outDir: OUT_DIR,
    emptyOutDir: true,
    rollupOptions: {
      input: {
        patient: path.resolve(import.meta.dirname, "patient.html"),
        practitioner: path.resolve(import.meta.dirname, "practitioner.html"),
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
    env: { TZ: "Asia/Kolkata" },
    setupFiles: ["src/test/setup.ts"],
  },
});
