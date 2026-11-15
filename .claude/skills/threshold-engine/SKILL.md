---
name: threshold-engine
description: Use when building or changing personal thresholds (R2), the 5% pre-breach band check, the batch update job (R1), or trend-based threshold revision. Covers the formulas, edge cases and tests.
---

# Threshold engine (R1, R2, R3 trigger)

## Personal threshold (R2)

Thresholds are computed per user per metric (fasting glucose, post-meal glucose, optionally HbA1c, BP).

```
base       = clinical_limits[metric][diabetes_status]        # from app/core/clinical_limits.py, cited
demo_adj   = age_band_adj + sex_adj                          # small, bounded, documented
risk_adj   = f(model_risk_tier)                              # higher risk -> tighter upper limit
trend_adj  = f(user's recent readings slope)                 # from trend analysis, bounded
threshold  = clamp(base + demo_adj + risk_adj + trend_adj, hard_min, hard_max)
```

Rules:
- Every adjustment is **bounded** (e.g. total ±15 mg/dL) and **clamped** to clinical hard limits.
  The ML can tighten or relax within a safe range; it can never push a limit into unsafe territory.
- Store each computed threshold with its components and `model_version` in `thresholds_history`
  so the dashboard can show *why* a limit is what it is.
- Glucose has **two** limits: an upper (hyperglycaemia) and a lower (hypoglycaemia) limit.

## Trend revision

- Per user: rolling 14-day readings -> linear regression slope (statsmodels OLS) + EWMA.
- Only revise when there are >= 7 readings and the slope's p-value < 0.05; otherwise keep the old threshold.
- Revisions are logged, never silent.

## Pre-breach band (R3 trigger) - get the direction right

```
upper limit U:  pre-breach if 0.95*U <= reading < U      breach if reading >= U
lower limit L:  pre-breach if L < reading <= 1.05*L      breach if reading <= L
```

Return a typed result: `Literal["normal", "pre_breach_high", "pre_breach_low", "breach_high", "breach_low"]`.
Band percentage (`0.05`) lives in config, not inline.

- `pre_breach_*` -> queue a voice advisory (web-push-voice-advisory skill).
- `breach_*` -> advisory **plus** practitioner escalation flag.
- De-duplicate: one advisory per user per metric per state per N hours (configurable cooldown).

## Batch update (R1)

`app/jobs/batch_update.py`, scheduled with APScheduler (and exposed as `POST /admin/batch-run` for the demo):
1. Load all active users + readings since last run.
2. Recompute risk tier (if profile changed) and thresholds.
3. Evaluate each new reading against the band; collect alerts.
4. Write results in one transaction; dispatch alerts after commit.
5. Idempotent: re-running for the same window must not duplicate alerts (use a `run_id` + unique constraint).

## Tests that must exist

- Exactly at 95% of U -> `pre_breach_high`; at 94.99% -> `normal`; at U -> `breach_high`.
- Mirror cases for L with 105%.
- Adjustment clamping at both ends.
- Batch job run twice -> same alert count.
