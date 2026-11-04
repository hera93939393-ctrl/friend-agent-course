"""크레딧 규칙. 원장(credit_ledger)에 쌓고, 잔액은 원장의 합계다. 차감은 서버(/api/chat)에서만 한다.

- 메시지 하나 = 크레딧 1개. 답을 만들기 전에(받자마자) 뺀다: 동시에 여러 요청을 보내 잔액보다 많이 쓰는 것을 막기 쉽다.
- 답을 만들다 실패하면 지우지 않고 되돌리는 줄(+1)을 쌓는다.
- 위기 표현에는 크레딧이 0 이어도 차감 없이 안전 응답을 한다(크레딧 안내로 막지 않는다).
- 처음 온 사용자에게 무료 크레딧을 한 번만 준다(ref signup:사용자ID 가 유일해서 두 번 쌓이지 않는다).
  게스트는 체험 3개만, 구글 계정에 연결하면 나머지를 준다(ensure_signup 설명 참고).
"""
import os
import uuid

from fastapi.responses import JSONResponse

from app.auth import LOCAL_USER

INSUFFICIENT_MESSAGE = "크레딧을 다 썼어요. 크레딧을 충전하면 계속 이야기할 수 있어요."

# 상품표. 금액과 지급 크레딧은 여기 한 곳에만 둔다. 화면은 상품 ID 만 서버로 보내고, 서버가 이 표에서 찾는다.
PRODUCTS = {
    "credits_100": {"name": "해피 크레딧 100개", "amount": 1000, "credits": 100},
    "credits_550": {"name": "해피 크레딧 550개", "amount": 5000, "credits": 550},
}


def product_list():
    return [{"id": pid, "name": p["name"], "amount": p["amount"], "credits": p["credits"],
             "per_credit": round(p["amount"] / p["credits"], 1)} for pid, p in PRODUCTS.items()]


def signup_credits():
    """계정(구글 연결) 하나가 받는 무료 크레딧 합계."""
    return int(os.environ.get("SIGNUP_CREDITS", "30"))


def guest_credits():
    """게스트(익명 로그인)가 먼저 받는 체험 크레딧. 게스트는 버튼 한 번으로 새로 만들 수 있어 작게 준다."""
    return min(int(os.environ.get("GUEST_CREDITS", "3")), signup_credits())


def ensure_signup(store, user):
    """처음 온 사용자에게 무료 크레딧을 쌓는다. ref 가 사용자마다 하나라 같은 사람이 두 번 받지 못한다.

    공격 시험에서 게스트를 새로 만들 때마다 30개를 받을 수 있었다(5명 → 150개). 그래서 나눠 준다.
    - 게스트: 체험 3개(signup:사용자ID)
    - 구글 계정에 연결하면(같은 사용자ID, 익명이 아님): 나머지 27개(account:사용자ID). 새 구글 계정은 쉽게 못 만든다
    - AUTH=none 의 개발용 사용자는 한 명뿐이라 새로 만들 수 없으므로 처음부터 30개
    """
    first = signup_credits() if user.id == LOCAL_USER else guest_credits()
    store.add_credit(user.id, first, "signup", f"signup:{user.id}")
    if not user.anonymous and signup_credits() > first:
        store.add_credit(user.id, signup_credits() - first, "account", f"account:{user.id}")


def spend(store, uid):
    """메시지 하나에 1개를 뺀다. (빠졌는지, 남은 잔액, 이 차감의 ref)"""
    ref = f"chat:{uid}:{uuid.uuid4()}"
    result = store.spend_credit(uid, ref)
    return result["ok"], result["balance"], ref


def refund(store, uid, ref):
    """뺀 크레딧을 되돌리는 줄을 쌓는다. 같은 차감은 한 번만 되돌린다(ref refund:… 가 유일)."""
    store.add_credit(uid, 1, "refund", f"refund:{ref}")


def insufficient(balance):
    """크레딧 부족은 402(Payment Required). 401·500·200 이 아니어야 화면과 모니터링이 제대로 알아본다."""
    return JSONResponse(status_code=402, content={"error": "insufficient_credits", "message": INSUFFICIENT_MESSAGE,
                                                  "balance": max(balance, 0)})


def summary(store, user, limit=10):
    ensure_signup(store, user)
    uid = user.id
    return {"balance": store.credit_balance(uid),
            "history": [{"delta": r["delta"], "reason": r["reason"], "created_at": r["created_at"]}
                        for r in store.credit_history(uid, limit)]}
