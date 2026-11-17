-- 0010_retrain_runs.sql
-- Phase 10 (R5): manual risk-model retraining, recorded in batch_runs.
--
-- One statement per change so tests/test_db_models.py can replay it on top of 0001-0009.
--
-- app/jobs/retrain.py trains a candidate model into a new versioned folder and promotes
-- it only when its test recall and precision hold up against the current model. Each
-- run records the decision and both sets of metrics here. Retraining never touches
-- thresholds_history: the risk tier is audit-only and never moves a glucose limit.

-- ---------------------------------------------------------------------------
-- batch_runs.retrain_decision: what a 'retrain' run did with its candidate.
--   promoted - the candidate became models/risk_model.* (previous one backed up)
--   kept     - the current model stays (metrics dropped, or training failed)
--   dry_run  - trained and compared, never promoted (--dry-run)
-- ---------------------------------------------------------------------------
alter table public.batch_runs
  add column retrain_decision text check (retrain_decision in ('promoted', 'kept', 'dry_run'));

-- batch_runs.retrain_report: old and new versions, test metrics, the reasons for the
-- decision and where the candidate and backup files are. Metrics only, no patient data.
alter table public.batch_runs
  add column retrain_report jsonb;

-- Only retrain runs carry these, and a finished retrain run always has a decision.
alter table public.batch_runs
  add constraint batch_runs_retrain_fields_only_on_retrain
  check (job = 'retrain' or (retrain_decision is null and retrain_report is null));
alter table public.batch_runs
  add constraint batch_runs_finished_retrain_has_decision
  check (job <> 'retrain' or status = 'running' or retrain_decision is not null);

comment on column public.batch_runs.retrain_decision is
  'R5 retrain runs only: promoted, kept or dry_run.';
comment on column public.batch_runs.retrain_report is
  'R5 retrain runs only: old/new model versions, test metrics and the promote/keep reasons.';
