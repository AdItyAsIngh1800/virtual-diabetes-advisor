-- 0003_conversation_turns_append_only.sql
-- R5 audit: conversation_turns (and the reviews attached to them) are append-only.
--
-- Two layers:
--   1. Privileges: the app roles (anon, authenticated, service_role) have no
--      UPDATE / DELETE / TRUNCATE on these tables. The backend may only INSERT/SELECT.
--   2. Triggers: reject UPDATE / DELETE / TRUNCATE from *any* role, including the
--      postgres connection used by SQLAlchemy (DATABASE_URL). A deliberate change of
--      policy has to drop the trigger in a reviewed migration, which leaves a trace.

revoke update, delete, truncate on public.conversation_turns from anon, authenticated, service_role;
revoke update, delete, truncate on public.turn_reviews       from anon, authenticated, service_role;
grant select, insert on public.conversation_turns to service_role;
grant select, insert on public.turn_reviews       to service_role;

create or replace function app_private.reject_audit_mutation()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception '% is append-only: % is not allowed', tg_table_name, tg_op
    using errcode = 'insufficient_privilege';
end;
$$;
revoke all on function app_private.reject_audit_mutation() from public;

create trigger conversation_turns_no_update_delete
  before update or delete on public.conversation_turns
  for each row execute function app_private.reject_audit_mutation();
create trigger conversation_turns_no_truncate
  before truncate on public.conversation_turns
  for each statement execute function app_private.reject_audit_mutation();

create trigger turn_reviews_no_update_delete
  before update or delete on public.turn_reviews
  for each row execute function app_private.reject_audit_mutation();
create trigger turn_reviews_no_truncate
  before truncate on public.turn_reviews
  for each statement execute function app_private.reject_audit_mutation();
