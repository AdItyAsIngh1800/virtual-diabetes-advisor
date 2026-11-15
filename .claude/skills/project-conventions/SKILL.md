---
name: project-conventions
description: Coding standards for the diabetes advisor repo. Use before writing or editing ANY Python or TypeScript file in this project - docstrings, type hints, comments, folder layout, config and error handling.
---

# Project conventions (Team 6 - Virtual Diabetes Advisor)

These are graded requirements, not suggestions. A PostToolUse hook runs ruff + mypy on every
`.py` file you touch and will send failures back to you; write code that passes first time.

## Python

- **Docstrings**: Google style on every module, class, public function and method.
  Include `Args:`, `Returns:`, `Raises:` (when it raises). One-line summary ends with a period.
- **Type hints**: every parameter and return, including `-> None`. `from __future__ import annotations`
  at the top of each module. No bare `Any` - use `TypedDict`, Pydantic models, `Literal`, or generics.
  DataFrames: annotate as `pd.DataFrame`; document expected columns in the docstring.
- **Runtime validation**: anything crossing a boundary (HTTP body, webhook payload, CSV row, DB row,
  model output) is parsed into a Pydantic v2 model. Static types are not enough at boundaries.
- **Comments**: explain *why*, not *what*. Mandatory for: every clinical number (cite the source,
  e.g. `# ADA 2025: fasting target 80-130 mg/dL for adults with diabetes`), every threshold formula,
  every magic constant, every place the ML output is adjusted by a rule.
- **Constants**: clinical limits live in `app/core/clinical_limits.py`, never inline.
- **Errors**: raise specific exceptions (`ThresholdComputationError`, `NotificationDeliveryError`),
  log with `logging.getLogger(__name__)`, never `print`. Never swallow exceptions silently.
- **Secrets**: only via `app/core/config.py` (pydantic-settings reading `.env`). Never hard-code.
- **Tests**: every module in `app/services/` gets a `tests/` file. Threshold and band logic need
  boundary tests (exactly 95%, exactly 100%, just below/above).

## Layout

```
app/
  api/            FastAPI routers (users, readings, webhooks, dashboard)
  core/           config.py, clinical_limits.py, logging.py, exceptions.py
  ml/             training, feature pipeline, model registry (joblib artifacts in models/)
  services/       thresholds.py, monitor.py, advisory.py, voice.py, webpush.py, chat.py, audit.py
  jobs/           batch_update.py (R1), retrain.py (R5)
  db/             SQLAlchemy models + Supabase migrations
notebooks/        Colab-style notebooks for MLE, linear regression, classification (course rubric)
web/              React + Vite + shadcn dashboard (R6)
tests/
```

## TypeScript (web/)

- `strict: true`, no `any`. Types for API responses generated from the FastAPI OpenAPI schema.
- JSDoc on exported components and hooks. Components use shadcn/ui via the shadcn MCP.

## Requirement tags

Tag code with the requirement it serves so the review/viva is easy: `# R3: pre-breach band check`.
