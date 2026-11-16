-- 0009_kb_upgrade.sql
-- Phase 9 (R5): knowledge-base upgrade loop, human in the loop.
--
-- One statement per change so tests/test_db_models.py can replay it on top of 0001-0008.
--
-- The weekly job (app/jobs/kb_upgrade.py) proposes DRAFT kb_entries from reviewed
-- answers and repeated "I don't know" questions. A draft's title is the patient's
-- question and its body is empty: the system never writes medical content. A
-- practitioner writes the body, cites a source and approves it; nothing is served
-- until then (0002 RLS and SqlChatStore.approved_kb_entries read approved rows only).

-- ---------------------------------------------------------------------------
-- kb_entries.review_hint: the reviewer's note (or "asked N times") that explains why
-- a draft was proposed. Staff-only working text, never patient-facing: RLS (0002)
-- shows patients approved rows only, and an approved row may not carry a hint.
-- ---------------------------------------------------------------------------
alter table public.kb_entries
  add column review_hint text;
alter table public.kb_entries
  add constraint kb_entries_hint_never_served
  check (status <> 'approved' or review_hint is null);

-- kb_entries.priority: the draft came from an answer a reviewer marked unsafe. The
-- Knowledge base tab lists priority drafts first. Approval clears it.
alter table public.kb_entries
  add column priority boolean not null default false;

-- An approved entry must say something and say where it comes from.
alter table public.kb_entries
  add constraint kb_entries_approved_has_text
  check (status <> 'approved' or (btrim(body) <> '' and btrim(coalesce(source_citation, '')) <> ''));

-- At most one live approved row per logical entry (the chat serves exactly one).
create unique index kb_entries_one_approved_per_key_idx
  on public.kb_entries (entry_key, language) where status = 'approved';
-- At most one open edit of an approved entry at a time.
create unique index kb_entries_one_draft_per_superseded_idx
  on public.kb_entries (supersedes_id) where status = 'draft' and supersedes_id is not null;
-- The job proposes a draft from a given turn at most once (idempotent re-runs).
create unique index kb_entries_one_proposal_per_turn_idx
  on public.kb_entries (proposed_from_turn_id)
  where origin = 'proposed_from_review' and proposed_from_turn_id is not null;

-- ---------------------------------------------------------------------------
-- Approved and retired rows are never edited in place. An approved row may only be
-- retired (when a new version supersedes it); a retired row never changes again.
-- The FK columns (approved_by, created_by, supersedes_id, proposed_from_turn_id) are
-- left out on purpose: ON DELETE SET NULL must still work on them.
-- ---------------------------------------------------------------------------
create or replace function app_private.kb_entries_freeze_published()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status in ('approved', 'retired') and (
       new.entry_key is distinct from old.entry_key
    or new.title is distinct from old.title
    or new.body is distinct from old.body
    or new.language is distinct from old.language
    or new.kb_version is distinct from old.kb_version
    or new.source_citation is distinct from old.source_citation
    or new.origin is distinct from old.origin
    or new.approved_at is distinct from old.approved_at
    or new.review_hint is distinct from old.review_hint
    or new.priority is distinct from old.priority
    or (old.status = 'approved' and new.status not in ('approved', 'retired'))
    or (old.status = 'retired' and new.status <> 'retired')
  ) then
    raise exception 'kb_entries row % is %: approved and retired entries are never edited in place', old.id, old.status
      using errcode = 'check_violation';
  end if;
  return new;
end;
$$;
create trigger kb_entries_freeze_published before update on public.kb_entries
  for each row execute function app_private.kb_entries_freeze_published();

-- ---------------------------------------------------------------------------
-- batch_runs: counts of a 'kb_upgrade' run (the other jobs leave them at 0).
-- ---------------------------------------------------------------------------
alter table public.batch_runs
  add column kb_candidates integer not null default 0 check (kb_candidates >= 0);
alter table public.batch_runs
  add column kb_drafts_proposed integer not null default 0 check (kb_drafts_proposed >= 0);

comment on column public.kb_entries.review_hint is
  'Why the KB upgrade job proposed this draft (reviewer note or repeat count). Staff only; NULL once approved.';
