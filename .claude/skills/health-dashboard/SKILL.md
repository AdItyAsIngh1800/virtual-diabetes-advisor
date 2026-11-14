---
name: health-dashboard
description: Use when building the React dashboard (R6) - healthcare data views, conversation/audit views, charts, or the API endpoints that feed them.
---

# Dashboard (R6)

Stack: React + Vite + TypeScript + Tailwind + shadcn/ui (add components via the shadcn MCP) + Recharts.
Before charting, load the `dataviz` skill. For the look, use `taste-skill:design-taste-frontend` once,
then stay consistent. Verify pages with the Playwright MCP (screenshot each view at 1280px and 390px).

## Roles

- **Practitioner/admin**: all assigned patients, alerts, handoffs, audit reviews, KB approvals.
- **Patient**: own readings, thresholds, advisories, chat history.
Enforce with Supabase Row Level Security, not just front-end hiding.

## Views

1. **Overview** - KPI tiles: active users, readings today, pre-breach alerts (24h), breaches, open handoffs,
   advisory delivery rate.
2. **Patient detail** - glucose line chart with the personal upper/lower threshold lines and the shaded
   5% pre-breach bands; risk tier + top model features; threshold history with the reason for each change.
3. **Alerts** - table: time, user (masked), metric, state, delivery status, escalated. Filter by state.
4. **Conversations** - turn list with intent, KB entries used, escalation flag, review buttons (R5).
5. **Knowledge base** - draft entries awaiting approval, approve/reject, version history.
6. **Model** - current model version, metrics (recall, ROC-AUC, PR-AUC), confusion matrix, last batch run.

## API contract

- FastAPI routers under `/api/dashboard/*`, Pydantic response models only.
- Generate TS types from `/openapi.json` (`openapi-typescript`) - never hand-write response types.
- Paginate every list endpoint (`limit`, `cursor`).

Use synthetic seeded patients for the demo (a `scripts/seed_demo.py` that creates ~20 users with
realistic reading curves, a few drifting into the pre-breach band) so the dashboard is never empty.
