-- 0005_batch_update.sql
-- Phase 4 (R1 batch update): demo-data flag, persisted risk assessments, and a
-- one-running-batch guard.

-- ---------------------------------------------------------------------------
-- users.is_demo: synthetic patients created by scripts/seed_demo.py. The seed script
-- deletes every is_demo user before re-seeding, so it must never be set on a real
-- patient. Partial index: demo rows are few, and only the seed script filters on it.
-- ---------------------------------------------------------------------------
alter table public.users
  add column is_demo boolean not null default false;
create index users_is_demo_idx on public.users (id) where is_demo;
comment on column public.users.is_demo is
  'Synthetic demo patient (scripts/seed_demo.py); deleted and recreated on every seed.';

-- ---------------------------------------------------------------------------
-- risk_assessments: every risk-model score of a user's BRFSS profile (R1/R2).
-- The batch job re-scores only when profile_hash (SHA-256 of the validated
-- RiskProfile) or the model version changes; otherwise it reuses the latest row.
-- A separate table, not profile columns: writing to profiles would bump
-- profiles.updated_at (trigger), which would itself look like a profile change.
-- ---------------------------------------------------------------------------
create table public.risk_assessments (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references public.users (id) on delete cascade,
  profile_hash   text not null check (profile_hash ~ '^[0-9a-f]{64}$'),
  model_version  text not null,
  -- Same precision as thresholds_history.risk_score.
  risk_score     numeric(5, 4) not null check (risk_score between 0 and 1),
  risk_tier      text not null check (risk_tier in ('low', 'moderate', 'high')),
  flagged        boolean not null,
  batch_run_id   uuid references public.batch_runs (id) on delete set null,
  created_at     timestamptz not null default now()
);
create index risk_assessments_user_created_idx
  on public.risk_assessments (user_id, created_at desc);
create index risk_assessments_batch_run_idx on public.risk_assessments (batch_run_id);
comment on table public.risk_assessments is
  'Risk-model scores per profile version; latest row per user is reused until the profile changes.';

-- Same access model as every per-patient table (0002_rls.sql): clients read their
-- accessible patients' rows, only the backend (service_role) writes. Supabase grants
-- ALL on new tables to anon/authenticated by default, so narrow it explicitly.
alter table public.risk_assessments enable row level security;
revoke all on public.risk_assessments from anon;
revoke insert, update, delete, truncate, references, trigger
  on public.risk_assessments from authenticated;
grant select on public.risk_assessments to authenticated;
create policy risk_assessments_select on public.risk_assessments
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

-- ---------------------------------------------------------------------------
-- batch_runs: at most one running execution per job. The scheduler and
-- POST /admin/batch-run can fire at the same time; the second insert of a 'running'
-- row fails on this index, and that job skips instead of double-processing (R1).
-- Works through Supabase's transaction pooler, unlike session advisory locks.
-- ---------------------------------------------------------------------------
create unique index batch_runs_one_running_idx
  on public.batch_runs (job) where status = 'running';
