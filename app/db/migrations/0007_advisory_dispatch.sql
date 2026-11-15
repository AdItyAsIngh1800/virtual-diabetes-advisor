-- 0007_advisory_dispatch.sql
-- Phase 6 (R3): the advisory dispatcher - voice audio, at-most-once Web Push, Realtime.
--
-- One statement per change so tests/test_db_models.py can replay it on top of 0001-0006.

-- ---------------------------------------------------------------------------
-- advisories: voice audio and the dispatch claim.
-- ---------------------------------------------------------------------------
-- Object path of the gTTS mp3 in the private 'advisory-audio' bucket, e.g.
-- '<user_id>/<advisory_id>.mp3'. NULL when TTS or the upload failed: the advisory page
-- then speaks the text with the browser's speechSynthesis instead.
alter table public.advisories add column audio_path text;

-- Set (with attempt_count + 1) by the one dispatcher run that wins the conditional
-- UPDATE ... WHERE delivery_status = 'queued' AND dispatch_claimed_at IS NULL, and
-- committed before the push is sent. A second, overlapping run finds the row claimed
-- and skips it, so an advisory is pushed at most once. A claim left by a crashed run
-- is never retried (that could push twice); it is marked failed instead.
alter table public.advisories add column dispatch_claimed_at timestamptz;

-- queued -> sent -> opened | failed | expired | suppressed.
-- 'suppressed': a push for the same user, limit and state was already sent within
-- ADVISORY_COOLDOWN_HOURS (R3 cooldown, checked again at send time). Level 2
-- hypoglycaemia (< 54 mg/dL) is never suppressed.
alter table public.advisories drop constraint advisories_delivery_status_check;
alter table public.advisories add constraint advisories_delivery_status_check
  check (delivery_status in ('queued', 'sent', 'opened', 'failed', 'expired', 'suppressed'));

-- The dispatcher's work list: queued rows only, oldest first.
create index advisories_dispatch_queue_idx on public.advisories (created_at)
  where delivery_status = 'queued';
-- Dispatch-time cooldown lookup: latest push per user/limit/state.
create index advisories_sent_cooldown_idx on public.advisories (user_id, limit_kind, state, sent_at desc)
  where sent_at is not null;

comment on column public.advisories.audio_path is
  'Voice advisory mp3 in the private advisory-audio bucket; served only via short-lived signed URLs.';
comment on column public.advisories.dispatch_claimed_at is
  'When a dispatcher run claimed this advisory (at-most-once push guard).';

-- ---------------------------------------------------------------------------
-- Storage: private bucket for voice advisories. No storage.objects policies are
-- created, so only the backend (service role) can read or write; patients get a
-- short-lived signed URL from GET /api/advisories/{id}. ~25 s of gTTS mp3 is ~200 KB.
-- ---------------------------------------------------------------------------
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('advisory-audio', 'advisory-audio', false, 1048576, array['audio/mpeg'])
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Realtime: the dispatcher broadcasts on 'advisories:<auth uid>' with private = true
-- (Broadcast REST API, app/services/realtime.py). A signed-in patient may join and
-- receive broadcasts on their own topic only. The payload carries just the advisory id
-- and state; the page then fetches the advisory through the API, which checks
-- ownership again.
-- ---------------------------------------------------------------------------
create policy advisories_realtime_receive on realtime.messages
  for select to authenticated
  using (
    realtime.messages.extension = 'broadcast'
    and (select realtime.topic()) = 'advisories:' || (select auth.uid())::text
  );
