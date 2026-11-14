-- 0002_rls.sql
-- Row Level Security.
--
-- Access model:
--   * anon           : no access to anything.
--   * authenticated  : dashboard / mobile clients signed in via Supabase Auth. READ-ONLY.
--                      - patient      -> only their own rows
--                      - practitioner -> only rows of patients in patient_assignments
--                      - admin        -> all patient rows
--   * service_role   : the FastAPI backend and batch jobs. Bypasses RLS (Supabase
--                      default) and does all writes, after Pydantic validation.
--
-- Helper functions live in app_private, which PostgREST does not expose, and are
-- SECURITY DEFINER so policies on users/practitioners do not recurse into each other.

create schema if not exists app_private;
revoke all on schema app_private from public;
grant usage on schema app_private to authenticated;

-- The public.users row of the signed-in Supabase Auth user (NULL if none / inactive).
create or replace function app_private.current_app_user_id()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select u.id
  from public.users u
  where u.auth_user_id = (select auth.uid())
    and u.is_active;
$$;

-- True when the caller is an active practitioner or admin.
create or replace function app_private.is_staff()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.users u
    where u.auth_user_id = (select auth.uid())
      and u.is_active
      and u.role in ('practitioner', 'admin')
  );
$$;

-- Every patient id the caller may read. Policies use
--   user_id in (select app_private.accessible_patient_ids())
-- so the set is computed once per statement, not once per row.
create or replace function app_private.accessible_patient_ids()
returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  -- Patient: themself.
  select u.id
  from public.users u
  where u.auth_user_id = (select auth.uid())
    and u.role = 'patient'
  union
  -- Practitioner: their assigned patients.
  select pa.patient_id
  from public.patient_assignments pa
  join public.practitioners p on p.id = pa.practitioner_id
  join public.users u on u.id = p.user_id
  where u.auth_user_id = (select auth.uid())
    and u.is_active
    and p.is_active
  union
  -- Admin: every patient.
  select pt.id
  from public.users pt
  where pt.role = 'patient'
    and exists (
      select 1 from public.users a
      where a.auth_user_id = (select auth.uid())
        and a.is_active
        and a.role = 'admin'
    );
$$;

revoke all on function app_private.current_app_user_id() from public;
revoke all on function app_private.is_staff() from public;
revoke all on function app_private.accessible_patient_ids() from public;
grant execute on function app_private.current_app_user_id() to authenticated;
grant execute on function app_private.is_staff() to authenticated;
grant execute on function app_private.accessible_patient_ids() to authenticated;

-- set_updated_at is a trigger helper, not an RPC.
revoke all on function public.set_updated_at() from public, anon, authenticated;

-- ---------------------------------------------------------------------------
-- Enable RLS everywhere (no policy = no rows for non-service roles).
-- ---------------------------------------------------------------------------
alter table public.users               enable row level security;
alter table public.profiles            enable row level security;
alter table public.practitioners       enable row level security;
alter table public.patient_assignments enable row level security;
alter table public.batch_runs          enable row level security;
alter table public.readings            enable row level security;
alter table public.thresholds_history  enable row level security;
alter table public.advisories          enable row level security;
alter table public.handoffs            enable row level security;
alter table public.kb_entries          enable row level security;
alter table public.conversation_turns  enable row level security;
alter table public.turn_reviews        enable row level security;

-- ---------------------------------------------------------------------------
-- Table privileges: clients read, backend writes.
-- Supabase grants ALL to anon/authenticated by default; narrow that.
-- ---------------------------------------------------------------------------
revoke all on all tables in schema public from anon;
revoke insert, update, delete, truncate, references, trigger
  on all tables in schema public from authenticated;
grant select on all tables in schema public to authenticated;

-- ---------------------------------------------------------------------------
-- Policies (SELECT only, for authenticated).
-- ---------------------------------------------------------------------------

-- users: own row, plus patients the caller can access.
create policy users_select on public.users
  for select to authenticated
  using (
    auth_user_id = (select auth.uid())
    or id in (select app_private.accessible_patient_ids())
  );

-- Per-patient tables: same rule, keyed on the patient column.
create policy profiles_select on public.profiles
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

create policy readings_select on public.readings
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

create policy thresholds_history_select on public.thresholds_history
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

create policy advisories_select on public.advisories
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

create policy handoffs_select on public.handoffs
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

create policy conversation_turns_select on public.conversation_turns
  for select to authenticated
  using (user_id in (select app_private.accessible_patient_ids()));

-- turn_reviews are internal QA notes: staff only, and only for accessible patients.
create policy turn_reviews_select on public.turn_reviews
  for select to authenticated
  using (
    (select app_private.is_staff())
    and exists (
      select 1 from public.conversation_turns t
      where t.id = turn_id
        and t.user_id in (select app_private.accessible_patient_ids())
    )
  );

-- practitioners: staff see the directory; a patient sees only practitioners assigned to them.
create policy practitioners_select on public.practitioners
  for select to authenticated
  using (
    (select app_private.is_staff())
    or id in (
      select pa.practitioner_id
      from public.patient_assignments pa
      where pa.patient_id = (select app_private.current_app_user_id())
    )
  );

-- patient_assignments: a patient sees their own assignments; a practitioner sees the
-- assignments of patients they can access (which includes their own); admin sees all.
create policy patient_assignments_select on public.patient_assignments
  for select to authenticated
  using (patient_id in (select app_private.accessible_patient_ids()));

-- kb_entries: approved knowledge is readable by any signed-in user; drafts and
-- retired rows only by staff (R5 human-in-the-loop review).
create policy kb_entries_select on public.kb_entries
  for select to authenticated
  using (status = 'approved' or (select app_private.is_staff()));

-- batch_runs: operational data, staff only.
create policy batch_runs_select on public.batch_runs
  for select to authenticated
  using ((select app_private.is_staff()));
