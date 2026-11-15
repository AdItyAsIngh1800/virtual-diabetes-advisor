-- 0006_web_push.sql
-- Phase 5, revised: WhatsApp is removed (Meta's Cloud API needs a registered, verified
-- business; this is a student project). Advisories (R3) go out as Web Push to the
-- patient web app, and chat (R4) happens inside that app.
--
-- One statement per change so tests/test_db_models.py can replay the renames, drops
-- and new CHECK lists on top of 0001 and keep the ORM drift check exact.

-- ---------------------------------------------------------------------------
-- advisories: channel-neutral delivery tracking.
-- ---------------------------------------------------------------------------
-- Push-service / channel message ids instead of WhatsApp wamids. Unique, as before.
alter table public.advisories rename column wamid to message_id;
alter table public.advisories rename column audio_wamid to audio_message_id;
alter table public.advisories rename constraint advisories_wamid_key to advisories_message_id_key;
alter table public.advisories
  rename constraint advisories_audio_wamid_key to advisories_audio_message_id_key;

-- free_form / template only described the WhatsApp 24 h window; every row is NULL.
alter table public.advisories drop column delivery_channel;
-- Web push has no delivery or read receipts; "opened" (notification tapped or the
-- advisory page viewed) replaces them. Every existing row is 'queued'.
alter table public.advisories drop column delivered_at;
alter table public.advisories drop column read_at;

-- Which NotificationChannel delivered it. Only Web Push today; widen the CHECK when a
-- second channel (e.g. email) is added.
alter table public.advisories
  add column channel text not null default 'webpush' check (channel in ('webpush'));
alter table public.advisories add column opened_at timestamptz;

-- queued -> sent -> opened | failed | expired. 'expired': the reading was older than
-- ADVISORY_MAX_AGE_MINUTES when the dispatcher got to it, so it is never sent.
alter table public.advisories drop constraint advisories_delivery_status_check;
alter table public.advisories add constraint advisories_delivery_status_check
  check (delivery_status in ('queued', 'sent', 'opened', 'failed', 'expired'));
alter table public.advisories add constraint advisories_opened_has_time
  check (delivery_status <> 'opened' or opened_at is not null);

comment on column public.advisories.message_id is
  'Id of the delivered notification (channel-specific); unique.';
comment on column public.advisories.opened_at is
  'When the patient tapped the notification or opened the advisory page.';

-- ---------------------------------------------------------------------------
-- profiles: the phone number is no longer how we reach a patient. Keep the column
-- (and its E.164 CHECK / UNIQUE) for practitioner contact, but make it optional.
-- last_inbound_at only measured the WhatsApp 24 h window.
-- ---------------------------------------------------------------------------
alter table public.profiles alter column phone_e164 drop not null;
alter table public.profiles drop column last_inbound_at;

-- ---------------------------------------------------------------------------
-- readings: readings reported in the web app replace WhatsApp text/voice. No row
-- uses the WhatsApp sources (all are 'seed').
-- ---------------------------------------------------------------------------
alter table public.readings drop constraint readings_source_check;
alter table public.readings add constraint readings_source_check
  check (source in ('web', 'dashboard', 'api', 'seed'));
comment on column public.readings.source_message_id is
  'Client-generated id of the chat message the reading came from (de-duplicates retries).';

-- ---------------------------------------------------------------------------
-- conversation_turns: still append-only (0003 triggers and grants are untouched;
-- renaming a column and swapping a CHECK are DDL, not row updates). New turns use
-- channel 'web'. 'whatsapp_text' / 'whatsapp_voice' stay allowed only because rows
-- written before this migration cannot be changed; the app never writes them.
-- ---------------------------------------------------------------------------
alter table public.conversation_turns rename column wamid to message_id;
alter table public.conversation_turns
  rename constraint conversation_turns_wamid_key to conversation_turns_message_id_key;
alter table public.conversation_turns drop constraint conversation_turns_channel_check;
alter table public.conversation_turns add constraint conversation_turns_channel_check
  check (channel in ('web', 'dashboard', 'whatsapp_text', 'whatsapp_voice'));
comment on column public.conversation_turns.message_id is
  'Client-generated message id (de-duplicates retried chat requests).';

-- ---------------------------------------------------------------------------
-- push_subscriptions: one row per browser/device that allowed notifications (R3).
-- endpoint + p256dh + auth come from PushSubscription.toJSON() in the browser.
-- A push answered 404/410 means the browser dropped the subscription: the backend
-- sets revoked_at and never uses that row again. Rows are kept (not deleted) so the
-- dashboard can show when alerts stopped reaching a patient.
-- ---------------------------------------------------------------------------
create table public.push_subscriptions (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid not null references public.users (id) on delete cascade,
  -- Push service URL (FCM, Mozilla, Apple). Unique: re-subscribing the same browser
  -- updates the row instead of creating a duplicate.
  endpoint         text not null unique check (endpoint like 'https://%'),
  -- Browser's P-256 public key and auth secret (base64url), used to encrypt payloads.
  p256dh           text not null,
  auth             text not null,
  user_agent       text,
  last_success_at  timestamptz,
  revoked_at       timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now()
);
create index push_subscriptions_user_active_idx
  on public.push_subscriptions (user_id) where revoked_at is null;
create trigger push_subscriptions_set_updated_at before update on public.push_subscriptions
  for each row execute function public.set_updated_at();
comment on table public.push_subscriptions is
  'Web Push subscriptions per patient device; revoked_at set when the push service answers 404/410.';

-- Same access model as every per-patient table (0002_rls.sql): a signed-in patient may
-- read their own subscriptions; only the backend (service_role) writes, after
-- validating the Supabase session. Supabase grants ALL on new tables by default.
alter table public.push_subscriptions enable row level security;
revoke all on public.push_subscriptions from anon;
revoke insert, update, delete, truncate, references, trigger
  on public.push_subscriptions from authenticated;
grant select on public.push_subscriptions to authenticated;
create policy push_subscriptions_select on public.push_subscriptions
  for select to authenticated
  using (user_id = (select app_private.current_app_user_id()));
