-- 0001_core_schema.sql
-- Core tables for the Virtual Diabetes Advisor (R1-R6).
--
-- Conventions:
--   * UUID primary keys (gen_random_uuid) everywhere: ids are not guessable and
--     can be created client-side by the batch job before insert.
--   * Allowed values are TEXT + CHECK rather than Postgres ENUMs, so adding a value
--     is a one-line constraint swap instead of ALTER TYPE. Every CHECK list mirrors
--     a Python Literal (app/ml/schema.py, app/core/clinical_limits.py, app/db/models.py).
--   * Glucose is stored in mg/dL, matching app/core/clinical_limits.py.
--   * This file is the source of truth; app/db/models.py mirrors it (never create_all).

-- ---------------------------------------------------------------------------
-- Shared trigger: keep updated_at honest without trusting callers.
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- users: one row per person (patient, practitioner or admin).
-- auth_user_id is NULL for WhatsApp-only patients who never log in to the dashboard.
-- ---------------------------------------------------------------------------
create table public.users (
  id            uuid primary key default gen_random_uuid(),
  auth_user_id  uuid unique references auth.users (id) on delete set null,
  role          text not null default 'patient'
                check (role in ('patient', 'practitioner', 'admin')),
  is_active     boolean not null default true,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);
comment on table public.users is 'Every person known to the system; role decides RLS visibility.';

-- ---------------------------------------------------------------------------
-- profiles: demographics + BRFSS RiskProfile fields (app/ml/schema.py) + WhatsApp state.
-- BRFSS fields are nullable so a patient can exist (e.g. first inbound WhatsApp
-- message) before onboarding is complete; RiskProfile validates them before scoring.
-- ---------------------------------------------------------------------------
create table public.profiles (
  user_id                  uuid primary key references public.users (id) on delete cascade,
  full_name                text,
  -- E.164: '+', country code 1-9, total 8-15 digits (ITU-T E.164 max 15 digits).
  phone_e164               text not null unique check (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  -- Languages the TTS advisory supports (whatsapp-voice-advisory skill: en, ta, hi).
  language                 text not null default 'en' check (language in ('en', 'ta', 'hi')),
  -- BRFSS covers adults only (age_to_bucket raises below 18).
  age_years                smallint check (age_years between 18 and 120),
  -- BRFSS coding, same as RiskProfile.sex: 0 = female, 1 = male.
  sex                      smallint check (sex in (0, 1)),
  -- Matches clinical_limits.DiabetesStatus.
  diabetes_status          text check (diabetes_status in ('diabetes', 'no_diabetes')),
  -- BRFSS 13-level age bucket derived from age_years (mirrors schema.age_to_bucket):
  -- 1 = 18-24, then one bucket per 5 years from 25, 13 = 80+.
  brfss_age_bucket         smallint generated always as (
                             case
                               when age_years is null then null
                               when age_years < 25 then 1
                               when age_years >= 80 then 13
                               else ((age_years - 25) / 5 + 2)::smallint
                             end
                           ) stored,
  -- BRFSS RiskProfile fields (ranges from schema.FEATURE_RANGES / BRFSS 2015 codebook).
  high_bp                  boolean,
  high_chol                boolean,
  chol_check               boolean,
  bmi                      numeric(4, 1) check (bmi between 12 and 98),
  smoker                   boolean,
  stroke                   boolean,
  heart_disease_or_attack  boolean,
  phys_activity            boolean,
  fruits                   boolean,
  veggies                  boolean,
  hvy_alcohol_consump      boolean,
  any_healthcare           boolean,
  no_doc_bc_cost           boolean,
  gen_hlth                 smallint check (gen_hlth between 1 and 5),
  ment_hlth                smallint check (ment_hlth between 0 and 30),
  phys_hlth                smallint check (phys_hlth between 0 and 30),
  diff_walk                boolean,
  education                smallint check (education between 1 and 6),
  income                   smallint check (income between 1 and 8),
  -- R3: WhatsApp 24-hour customer-service window is measured from this.
  last_inbound_at          timestamptz,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);
comment on column public.profiles.last_inbound_at is
  'Last inbound WhatsApp message; free-form replies allowed only within 24h (R3).';

-- ---------------------------------------------------------------------------
-- practitioners + patient_assignments: who may see which patient (RLS, R4).
-- ---------------------------------------------------------------------------
create table public.practitioners (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null unique references public.users (id) on delete restrict,
  full_name    text not null,
  specialty    text,
  phone_e164   text unique check (phone_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  email        text,
  languages    text[] not null default array['en']::text[],
  is_active    boolean not null default true,
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now()
);

create table public.patient_assignments (
  practitioner_id  uuid not null references public.practitioners (id) on delete cascade,
  patient_id       uuid not null references public.users (id) on delete cascade,
  assigned_at      timestamptz not null default now(),
  primary key (practitioner_id, patient_id)
);
create index patient_assignments_patient_idx on public.patient_assignments (patient_id);
comment on table public.patient_assignments is
  'A practitioner sees a patient''s rows only while a row exists here.';

-- ---------------------------------------------------------------------------
-- batch_runs: one row per scheduled/manual job execution (R1, R5).
-- ---------------------------------------------------------------------------
create table public.batch_runs (
  id                  uuid primary key default gen_random_uuid(),
  job                 text not null check (job in ('batch_update', 'retrain', 'kb_upgrade')),
  triggered_by        text not null default 'scheduler'
                      check (triggered_by in ('scheduler', 'admin_api', 'manual')),
  status              text not null default 'running'
                      check (status in ('running', 'succeeded', 'failed')),
  window_start        timestamptz,
  window_end          timestamptz,
  model_version       text,
  users_processed     integer not null default 0 check (users_processed >= 0),
  readings_evaluated  integer not null default 0 check (readings_evaluated >= 0),
  thresholds_written  integer not null default 0 check (thresholds_written >= 0),
  advisories_created  integer not null default 0 check (advisories_created >= 0),
  error               text,
  started_at          timestamptz not null default now(),
  finished_at         timestamptz,
  check (window_end is null or window_start is null or window_end > window_start),
  check ((status = 'running') = (finished_at is null))
);
create index batch_runs_job_started_idx on public.batch_runs (job, started_at desc);

-- ---------------------------------------------------------------------------
-- readings: glucose measurements reported by the patient.
-- ---------------------------------------------------------------------------
create table public.readings (
  id                 uuid primary key default gen_random_uuid(),
  user_id            uuid not null references public.users (id) on delete cascade,
  -- Which limit set applies: fasting -> fasting_high + low, post_meal -> post_meal_high + low,
  -- random -> low only (no cited upper target for untimed readings).
  context            text not null check (context in ('fasting', 'post_meal', 'random')),
  -- Sanity bounds, not clinical limits: home meters read roughly 20-600 mg/dL and
  -- show LO/HI outside that, so anything outside 10-1000 is a typo or unit mix-up.
  value_mg_dl        numeric(5, 1) not null check (value_mg_dl between 10 and 1000),
  measured_at        timestamptz not null,
  source             text not null
                     check (source in ('whatsapp_text', 'whatsapp_voice', 'dashboard', 'api', 'seed')),
  -- Inbound WhatsApp message id: Meta retries webhooks, so this de-duplicates.
  source_message_id  text unique,
  created_at         timestamptz not null default now()
);
create index readings_user_measured_idx on public.readings (user_id, measured_at desc);
create index readings_created_idx on public.readings (created_at);

-- ---------------------------------------------------------------------------
-- thresholds_history: every computed personal threshold with its components (R2).
-- threshold = clamp(base + demo_adj + risk_adj + trend_adj, hard_min, hard_max)
-- (threshold-engine skill). Rows are never updated; the latest row per
-- (user_id, limit_kind) is the current threshold (see current_thresholds view).
-- ---------------------------------------------------------------------------
create table public.thresholds_history (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references public.users (id) on delete cascade,
  limit_kind        text not null check (limit_kind in ('fasting_high', 'post_meal_high', 'low')),
  direction         text not null check (direction in ('upper', 'lower')),
  diabetes_status   text not null check (diabetes_status in ('diabetes', 'no_diabetes')),
  base_mg_dl        numeric(5, 1) not null check (base_mg_dl > 0),
  demo_adj_mg_dl    numeric(5, 1) not null default 0,
  risk_adj_mg_dl    numeric(5, 1) not null default 0,
  trend_adj_mg_dl   numeric(5, 1) not null default 0,
  unclamped_mg_dl   numeric(5, 1) not null,
  hard_min_mg_dl    numeric(5, 1) not null,
  hard_max_mg_dl    numeric(5, 1) not null,
  threshold_mg_dl   numeric(5, 1) not null,
  -- Risk model output that drove risk_adj.
  risk_score        numeric(5, 4) check (risk_score between 0 and 1),
  risk_tier         text check (risk_tier in ('low', 'moderate', 'high')),
  model_version     text not null,
  -- Trend revision inputs (14-day OLS slope; revise only if n >= 7 and p < 0.05).
  trend_slope_mg_dl_per_day  numeric(7, 3),
  trend_p_value     numeric(6, 5) check (trend_p_value between 0 and 1),
  trend_n_readings  integer check (trend_n_readings >= 0),
  reason            text not null
                    check (reason in ('initial', 'batch', 'trend_revision', 'profile_change')),
  source_citation   text not null,
  batch_run_id      uuid references public.batch_runs (id) on delete set null,
  effective_from    timestamptz not null default now(),
  created_at        timestamptz not null default now(),
  check (hard_min_mg_dl <= hard_max_mg_dl),
  -- Hard safety net: whatever the ML does, a stored threshold is inside its clamp.
  check (threshold_mg_dl between hard_min_mg_dl and hard_max_mg_dl),
  check ((limit_kind = 'low') = (direction = 'lower'))
);
create index thresholds_history_current_idx
  on public.thresholds_history (user_id, limit_kind, effective_from desc);
create index thresholds_history_batch_run_idx on public.thresholds_history (batch_run_id);

-- Latest threshold per user and limit. security_invoker so the caller's RLS applies.
create view public.current_thresholds
with (security_invoker = true) as
select distinct on (user_id, limit_kind) *
from public.thresholds_history
order by user_id, limit_kind, effective_from desc, created_at desc;

-- ---------------------------------------------------------------------------
-- advisories: one alert raised by the band check, and its WhatsApp delivery (R3).
-- ---------------------------------------------------------------------------
create table public.advisories (
  id                    uuid primary key default gen_random_uuid(),
  user_id               uuid not null references public.users (id) on delete cascade,
  reading_id            uuid not null references public.readings (id) on delete cascade,
  threshold_id          uuid not null references public.thresholds_history (id) on delete restrict,
  batch_run_id          uuid references public.batch_runs (id) on delete set null,
  limit_kind            text not null check (limit_kind in ('fasting_high', 'post_meal_high', 'low')),
  -- Band state from the threshold engine; 'normal' readings never create an advisory.
  state                 text not null
                        check (state in ('pre_breach_high', 'pre_breach_low', 'breach_high', 'breach_low')),
  reading_value_mg_dl   numeric(5, 1) not null,
  threshold_mg_dl       numeric(5, 1) not null,
  language              text not null check (language in ('en', 'ta', 'hi')),
  message_text          text not null,
  -- breach_* also escalates to a practitioner (threshold-engine skill).
  escalated             boolean not null default false,
  -- free_form inside the 24h window, otherwise the approved utility template.
  delivery_channel      text check (delivery_channel in ('free_form', 'template')),
  delivery_status       text not null default 'queued'
                        check (delivery_status in ('queued', 'sent', 'delivered', 'read', 'failed')),
  -- wamid of the text/template message; audio_wamid of the voice note. Status webhooks
  -- arrive keyed by wamid, hence unique.
  wamid                 text unique,
  audio_wamid           text unique,
  attempt_count         smallint not null default 0 check (attempt_count between 0 and 10),
  delivery_error_code   text,
  delivery_error        text,
  sent_at               timestamptz,
  delivered_at          timestamptz,
  read_at               timestamptz,
  failed_at             timestamptz,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  -- R1 idempotency: re-running the batch over the same readings cannot raise the
  -- same alert twice (one advisory per reading per limit).
  constraint advisories_reading_limit_key unique (reading_id, limit_kind),
  check (state not like 'breach_%' or escalated),
  check (delivery_status <> 'failed' or failed_at is not null)
);
-- R3 cooldown lookup: latest advisory for a user/limit/state.
create index advisories_cooldown_idx on public.advisories (user_id, limit_kind, state, created_at desc);
create index advisories_status_idx on public.advisories (delivery_status, created_at desc);
create index advisories_threshold_idx on public.advisories (threshold_id);
create index advisories_batch_run_idx on public.advisories (batch_run_id);

-- ---------------------------------------------------------------------------
-- handoffs: practitioner connect (R4). open -> assigned -> resolved.
-- ---------------------------------------------------------------------------
create table public.handoffs (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users (id) on delete cascade,
  practitioner_id  uuid references public.practitioners (id) on delete set null,
  advisory_id      uuid references public.advisories (id) on delete set null,
  trigger          text not null
                   check (trigger in ('practitioner_request', 'red_flag', 'breach_escalation')),
  status           text not null default 'open' check (status in ('open', 'assigned', 'resolved')),
  reason           text,
  resolution_note  text,
  opened_at        timestamptz not null default now(),
  assigned_at      timestamptz,
  resolved_at      timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  check (status <> 'assigned' or (practitioner_id is not null and assigned_at is not null)),
  check ((status = 'resolved') = (resolved_at is not null))
);
-- One live handoff per patient: messages relay to it while open/assigned.
create unique index handoffs_one_live_per_user
  on public.handoffs (user_id) where status in ('open', 'assigned');
create index handoffs_practitioner_status_idx on public.handoffs (practitioner_id, status);
create index handoffs_advisory_idx on public.handoffs (advisory_id);

-- ---------------------------------------------------------------------------
-- kb_entries: knowledge base, human-approved before it is served (R5).
-- Each row is one version of a logical entry (entry_key). Edits create a new
-- draft row that supersedes the old one; approved rows are not edited in place.
-- ---------------------------------------------------------------------------
create table public.kb_entries (
  id                     uuid primary key default gen_random_uuid(),
  entry_key              text not null,
  title                  text not null,
  body                   text not null,
  language               text not null default 'en' check (language in ('en', 'ta', 'hi')),
  status                 text not null default 'draft' check (status in ('draft', 'approved', 'retired')),
  -- KB release this row was approved into; each turn stores the kb_version that answered it.
  kb_version             integer check (kb_version > 0),
  source_citation        text,
  origin                 text not null default 'manual'
                         check (origin in ('manual', 'proposed_from_review')),
  supersedes_id          uuid references public.kb_entries (id) on delete set null,
  created_by             uuid references public.practitioners (id) on delete set null,
  approved_by            uuid references public.practitioners (id) on delete set null,
  approved_at            timestamptz,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  -- "Never auto-publish": an approved entry must name the practitioner who approved it.
  check (status <> 'approved' or (approved_by is not null and approved_at is not null and kb_version is not null)),
  constraint kb_entries_key_version_lang_key unique (entry_key, kb_version, language)
);
create index kb_entries_status_idx on public.kb_entries (status, kb_version);
create index kb_entries_supersedes_idx on public.kb_entries (supersedes_id);
create index kb_entries_created_by_idx on public.kb_entries (created_by);
create index kb_entries_approved_by_idx on public.kb_entries (approved_by);

-- ---------------------------------------------------------------------------
-- conversation_turns: audit log of every chat turn (R5). Append-only (0003).
-- ---------------------------------------------------------------------------
create table public.conversation_turns (
  id                    uuid primary key default gen_random_uuid(),
  -- RESTRICT, not CASCADE: audit rows must not vanish when a user is deleted.
  user_id               uuid not null references public.users (id) on delete restrict,
  handoff_id            uuid references public.handoffs (id) on delete restrict,
  advisory_id           uuid references public.advisories (id) on delete restrict,
  channel               text not null check (channel in ('whatsapp_text', 'whatsapp_voice', 'dashboard')),
  direction             text not null check (direction in ('inbound', 'outbound')),
  raw_text              text,
  transcript            text,
  intent                text check (intent in
                          ('reading_report', 'question', 'symptom_red_flag', 'practitioner_request', 'other')),
  kb_ids_used           uuid[] not null default '{}',
  kb_version            integer check (kb_version > 0),
  retrieval_confidence  numeric(5, 4) check (retrieval_confidence between 0 and 1),
  model                 text,
  model_version         text,
  response_text         text,
  latency_ms            integer check (latency_ms >= 0),
  escalated             boolean not null default false,
  -- WhatsApp message id of the inbound message (webhook de-duplication).
  wamid                 text unique,
  created_at            timestamptz not null default now()
);
create index conversation_turns_user_created_idx on public.conversation_turns (user_id, created_at desc);
create index conversation_turns_handoff_idx on public.conversation_turns (handoff_id);
create index conversation_turns_advisory_idx on public.conversation_turns (advisory_id);
create index conversation_turns_kb_version_idx on public.conversation_turns (kb_version);

-- turn_reviews: practitioner verdicts on turns. Separate table because turns are
-- append-only; a review is a new fact about a turn, not an edit of it.
create table public.turn_reviews (
  id           uuid primary key default gen_random_uuid(),
  turn_id      uuid not null references public.conversation_turns (id) on delete restrict,
  reviewer_id  uuid not null references public.practitioners (id) on delete restrict,
  verdict      text not null check (verdict in ('correct', 'incorrect', 'unsafe', 'needs_kb_update')),
  note         text,
  created_at   timestamptz not null default now()
);
create index turn_reviews_turn_idx on public.turn_reviews (turn_id);
create index turn_reviews_reviewer_idx on public.turn_reviews (reviewer_id);
create index turn_reviews_verdict_idx on public.turn_reviews (verdict, created_at desc);

-- kb_entries can point back at the turn whose review proposed it (R5 upgrade loop).
alter table public.kb_entries
  add column proposed_from_turn_id uuid references public.conversation_turns (id) on delete set null;
create index kb_entries_proposed_from_turn_idx on public.kb_entries (proposed_from_turn_id);

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------
create trigger users_set_updated_at before update on public.users
  for each row execute function public.set_updated_at();
create trigger profiles_set_updated_at before update on public.profiles
  for each row execute function public.set_updated_at();
create trigger practitioners_set_updated_at before update on public.practitioners
  for each row execute function public.set_updated_at();
create trigger advisories_set_updated_at before update on public.advisories
  for each row execute function public.set_updated_at();
create trigger handoffs_set_updated_at before update on public.handoffs
  for each row execute function public.set_updated_at();
create trigger kb_entries_set_updated_at before update on public.kb_entries
  for each row execute function public.set_updated_at();
