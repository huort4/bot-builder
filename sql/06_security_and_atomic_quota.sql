-- Phase 8: security hardening + atomic quota counter
-- Run AFTER 05_user_prompts_modes.sql. Idempotent.

-- 1) Row Level Security ON for every bot table, with NO policies.
--    Only the service_role key (used by the bot backend) can read/write.
--    The public anon/authenticated keys get nothing.
do $$
declare t text;
begin
  foreach t in array array['users','conversations','messages','usage','user_api_keys','user_quotas','user_prompts']
  loop
    if to_regclass('public.' || t) is not null then
      execute format('alter table public.%I enable row level security', t);
    end if;
  end loop;
end $$;

-- Earlier scripts (03, 05) disabled RLS on two tables; that is now reverted.
-- IMPORTANT: SUPABASE_KEY in the bot MUST be the service_role key.
-- Never "fix" permission errors by disabling RLS.

-- 2) Atomic quota increment (no read-modify-write race between concurrent requests)
create or replace function public.bump_quota(
  p_user_id bigint,
  p_tokens int,
  p_default_daily int default 50000,
  p_default_lifetime int default 3000000
)
returns table (
  o_used_today int,
  o_lifetime_used int,
  o_daily_limit int,
  o_lifetime_limit int
)
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.user_quotas (user_id, daily_limit, lifetime_limit)
  values (p_user_id, p_default_daily, p_default_lifetime)
  on conflict (user_id) do nothing;

  return query
  update public.user_quotas q
     set used_today = case when q.reset_at < current_date
                           then p_tokens
                           else q.used_today + p_tokens end,
         reset_at = current_date,
         lifetime_used = q.lifetime_used + p_tokens,
         updated_at = now()
   where q.user_id = p_user_id
  returning q.used_today, q.lifetime_used, q.daily_limit, q.lifetime_limit;
end;
$$;

-- Only the backend (service_role) may call it.
revoke all on function public.bump_quota(bigint, int, int, int) from public, anon, authenticated;
grant execute on function public.bump_quota(bigint, int, int, int) to service_role;
