---
name: brfss-model
description: Use when loading, exploring, cleaning or training on the BRFSS 2015 diabetes health indicator CSVs, building the risk model, or writing the course notebook (MLE, linear regression, classification).
---

# BRFSS 2015 model

## The three CSVs - which one for what

| File | Target | Rows | Use it for |
|---|---|---|---|
| `diabetes_012_health_indicators_BRFSS2015.csv` | `Diabetes_012` (0 none, 1 prediabetes, 2 diabetes) | ~253k, imbalanced | Final 3-class risk tier used by the threshold engine |
| `diabetes_binary_5050split_health_indicators_BRFSS2015.csv` | `Diabetes_binary` | ~70k, balanced | Fast experiments, model comparison, notebook demos |
| `diabetes_binary_health_indicators_BRFSS2015.csv` | `Diabetes_binary` | ~253k, real prevalence | Honest evaluation + probability calibration |

Rule: tune on the 50/50 file, **evaluate and calibrate on the real-prevalence file**. Balanced-data
accuracy overstates real-world performance; say so in the report.

## Critical caveat - say this in the viva

BRFSS is a **survey**: 21 features (HighBP, HighChol, BMI, Smoker, PhysActivity, GenHlth, Age bucket
1-13, Sex, Education, Income ...). It has **no glucose readings**. So:
- The model outputs a **risk probability / tier** per user from their profile.
- That risk tier **adjusts** the personal threshold (R2). It does not replace clinical limits.
- Glucose readings for R1/R3 come from users (web app form or chat) or a seeded simulator for the demo.

## Pipeline

1. Load with explicit dtypes; assert no nulls; drop exact duplicates (there are ~24k) and report the count.
2. Ordinal features (`Age`, `GenHlth`, `Education`, `Income`) stay ordinal - do not one-hot.
3. `BMI`, `MentHlth`, `PhysHlth` - check outliers, keep a `RobustScaler` for linear models.
4. Stratified train/val/test split, fixed `random_state=42`.
5. Models, in this order (matches the syllabus):
   - Logistic regression (baseline, coefficients explainable; tie to MLE: it maximises Bernoulli log-likelihood).
   - Random forest, XGBoost (`scale_pos_weight` or class weights for the imbalanced files).
6. Metrics: recall on the diabetic class first (missing a diabetic is the costly error), then
   ROC-AUC, PR-AUC, F1, confusion matrix. Accuracy alone is not acceptable.
7. Calibrate the chosen model (`CalibratedClassifierCV`, isotonic) on the real-prevalence file -
   the threshold engine consumes probabilities, so they must mean something.
8. Explain with SHAP or permutation importance; save the top features for the dashboard.
9. Save `models/risk_model.joblib` + `models/risk_model.meta.json` (version, date, features, metrics).

## Notebook rubric (course)

The Colab notebook must visibly cover:
- **MLE**: derive/estimate a Gaussian's mean/var for BMI by maximising log-likelihood; show it equals sample mean/var. Then logistic regression as MLE.
- **Linear regression**: e.g. predict `GenHlth` or `BMI` from other features; report coefficients, R², residual plot.
- **Classification**: the diabetes models above with full metrics.

Use `error-analysis` / `ds-thinking` commands for model critique; `mlflow-helper` if logging runs.
