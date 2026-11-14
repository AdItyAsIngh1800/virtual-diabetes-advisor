-- 0004_thresholds_history_ewma.sql
-- Store the EWMA glucose level behind each threshold (R2 trend revision), so the
-- dashboard (R6) can show the smoothed level next to the 14-day slope.

-- numeric(5, 1) mg/dL like every glucose column. Nullable: there is no level when the
-- trend window held no readings. A weighted mean of readings (10-1000 mg/dL, see
-- readings.value_mg_dl) is always positive.
alter table public.thresholds_history
  add column ewma_mg_dl numeric(5, 1) check (ewma_mg_dl > 0);

comment on column public.thresholds_history.ewma_mg_dl is
  'Time-weighted EWMA (3-day half-life) of the readings behind the trend fields (R2).';

-- current_thresholds was created with `select *`, which Postgres expands to a fixed
-- column list when the view is created, so it would not expose the new column.
-- Re-create it with the identical definition; CREATE OR REPLACE may append columns,
-- and it keeps the view's grants and security_invoker setting.
create or replace view public.current_thresholds
with (security_invoker = true) as
select distinct on (user_id, limit_kind) *
from public.thresholds_history
order by user_id, limit_kind, effective_from desc, created_at desc;
