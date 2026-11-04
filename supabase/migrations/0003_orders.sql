-- 크레딧 결제 주문. 화면이 결제를 시작하기 전에 서버가 주문을 대기(pending)로 저장하고,
-- 토스페이먼츠 승인이 끝나면 완료(done)로 바꾼 뒤 credit_ledger 에 +크레딧(ref order:주문번호)을 쌓는다.
-- 0002_credits.sql 다음에 staging(시험용) 프로젝트부터 SQL Editor 에서 실행한다.
--
-- 금액과 크레딧 수는 화면이 보낸 값이 아니라 서버 상품표(app/credits.py PRODUCTS)에서 찾아 저장한 값이다.
-- 0001, 0002 와 같이 RLS 를 켜고 정책은 만들지 않는다. 서버(secret key)만 읽고 쓴다.

create table if not exists public.orders (
  id bigint generated always as identity primary key,
  order_id text not null unique,                         -- 토스페이먼츠에 보내는 주문 번호. 추측할 수 없는 무작위 값
  user_id uuid not null references auth.users (id) on delete cascade,
  product_id text not null,
  amount integer not null check (amount > 0),            -- 원. 승인 때 이 값과 비교한다
  credits integer not null check (credits > 0),          -- 승인되면 원장에 쌓을 크레딧 수
  status text not null default 'pending' check (status in ('pending', 'done', 'failed')),
  payment_key text,                                      -- 토스페이먼츠 결제 키(승인 뒤)
  approved_at timestamptz,                               -- 토스페이먼츠가 승인한 시각. 문서용 테스트 키로는 이것이 승인 기록
  fail_code text,                                        -- 실패 때 토스페이먼츠가 돌려준 오류 코드
  created_at timestamptz not null default now()
);
create index if not exists orders_user_id_idx on public.orders (user_id, id);

alter table public.orders enable row level security;
