-- 크레딧 원장. 잔액 칸 하나를 고치는 대신, 크레딧이 늘고 준 기록을 한 줄씩 쌓기만 한다.
-- 잔액 = 그 사용자의 delta 를 모두 더한 값. 기록은 고치거나 지우지 않고, 잘못 뺐으면 되돌리는 줄(+1)을 새로 쌓는다.
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행한다. 0001_init.sql 다음에 staging(시험용) 프로젝트부터 실행한다.
--
-- 0001 과 같이 RLS를 켜고 정책은 만들지 않는다. 서버(secret key)만 읽고 쓴다.

create table if not exists public.credit_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  delta integer not null check (delta <> 0),          -- +30 가입, -1 메시지, +1 되돌림, +100 결제
  reason text not null,                                -- signup, chat, refund, purchase
  ref text not null unique,                            -- 무엇 때문인지. 유일해서 같은 결제·같은 메시지로 두 번 쌓이지 않는다
  created_at timestamptz not null default now()
);
create index if not exists credit_ledger_user_id_idx on public.credit_ledger (user_id, id);

alter table public.credit_ledger enable row level security;

-- 메시지 하나에 크레딧 1개를 뺀다. 잔액 확인과 -1 쌓기를 한 번에 해야, 같은 사용자가 요청을 동시에 여러 개 보내도
-- 잔액보다 많이 쓰지 못한다. 사용자마다 잠금(advisory lock)을 걸어 그 사용자의 차감은 한 번에 하나씩만 처리한다.
create or replace function public.spend_credit(p_user_id uuid, p_ref text, p_reason text default 'chat')
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  current_balance integer;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  select coalesce(sum(delta), 0) into current_balance from public.credit_ledger where user_id = p_user_id;
  if current_balance < 1 then
    return jsonb_build_object('ok', false, 'balance', current_balance);
  end if;
  insert into public.credit_ledger (user_id, delta, reason, ref) values (p_user_id, -1, p_reason, p_ref);
  return jsonb_build_object('ok', true, 'balance', current_balance - 1);
end;
$$;

-- public 스키마의 함수는 Data API 로 누구나 부를 수 있게 열린다. 브라우저(anon, authenticated)가
-- 남의 크레딧을 빼지 못하게 실행 권한을 거두고, 서버가 쓰는 service_role 에만 준다.
revoke all on function public.spend_credit(uuid, text, text) from public, anon, authenticated;
grant execute on function public.spend_credit(uuid, text, text) to service_role;
