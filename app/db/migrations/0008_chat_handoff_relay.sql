-- 0008_chat_handoff_relay.sql
-- Phase 7 (R4): two-way help chat and practitioner connect.
--
-- One statement per change so tests/test_db_models.py can replay it on top of 0001-0007.
--
-- Turn model (all rows channel 'web', append-only as before):
--   * patient message      direction 'inbound',  raw_text/transcript + the bot's response_text
--   * practitioner reply   direction 'outbound', response_text, author_practitioner_id set
-- Every row keeps user_id = the patient, so RLS (0002) shows the whole thread to the
-- patient and to their assigned practitioners, and nothing to anyone else.

-- ---------------------------------------------------------------------------
-- conversation_turns: who wrote a practitioner reply. RESTRICT like every audit FK:
-- an audited reply must keep pointing at its author. Adding a column is DDL, not a
-- row update, so the 0003 append-only triggers are untouched.
-- ---------------------------------------------------------------------------
alter table public.conversation_turns
  add column author_practitioner_id uuid references public.practitioners (id) on delete restrict;
-- Only replies (outbound) have a human author; patient messages never do.
alter table public.conversation_turns
  add constraint conversation_turns_author_is_outbound
  check (author_practitioner_id is null or direction = 'outbound');
create index conversation_turns_author_idx on public.conversation_turns (author_practitioner_id);

comment on column public.conversation_turns.author_practitioner_id is
  'Practitioner who wrote this outbound reply (handoff relay, R4); NULL for bot replies.';

-- ---------------------------------------------------------------------------
-- Live-handoff lookups: the practitioner page lists open/assigned handoffs newest
-- first. handoffs_one_live_per_user (0001) already guarantees one live row per patient.
-- ---------------------------------------------------------------------------
create index handoffs_live_opened_idx on public.handoffs (opened_at desc)
  where status in ('open', 'assigned');

-- ---------------------------------------------------------------------------
-- Realtime: the backend broadcasts (REST, service role) on two more private topics:
--   'chat:<auth uid>'      a practitioner replied to this patient
--   'handoffs:<auth uid>'  a new or updated handoff for this practitioner
-- Payloads carry ids only; pages re-fetch through the API, which checks access again.
-- A signed-in user may join only topics named after their own auth uid.
-- ---------------------------------------------------------------------------
create policy chat_realtime_receive on realtime.messages
  for select to authenticated
  using (
    realtime.messages.extension = 'broadcast'
    and (select realtime.topic()) in (
      'chat:' || (select auth.uid())::text,
      'handoffs:' || (select auth.uid())::text
    )
  );
