"""크레딧 규칙. 원장(credit_ledger)에 쌓고, 잔액은 원장의 합계다. 차감은 서버(/api/chat)에서만 한다.

- 메시지 하나 = 크레딧 1개. 답을 만들기 전에(받자마자) 뺀다: 동시에 여러 요청을 보내 잔액보다 많이 쓰는 것을 막기 쉽다.
- 답을 만들다 실패하면 지우지 않고 되돌리는 줄(+1)을 쌓는다.
- 위기 표현에는 크레딧이 0 이어도 차감 없이 안전 응답을 한다(크레딧 안내로 막지 않는다).
- 처음 온 사용자에게 무료 크레딧을 한 번만 준다(ref signup:사용자ID 가 유일해서 두 번 쌓이지 않는다).
"""
import os
import uuid

from fastapi.responses import JSONResponse

INSUFFICIENT_MESSAGE = "크레딧을 다 썼어요. 크레딧을 충전하면 계속 이야기할 수 있어요."


def signup_credits():
    return int(os.environ.get("SIGNUP_CREDITS", "30"))


def ensure_signup(store, uid):
    """처음 온 사용자면 무료 크레딧을 쌓는다. 이미 받았으면 아무 일도 없다."""
    store.add_credit(uid, signup_credits(), "signup", f"signup:{uid}")


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


def summary(store, uid, limit=10):
    ensure_signup(store, uid)
    return {"balance": store.credit_balance(uid),
            "history": [{"delta": r["delta"], "reason": r["reason"], "created_at": r["created_at"]}
                        for r in store.credit_history(uid, limit)]}
