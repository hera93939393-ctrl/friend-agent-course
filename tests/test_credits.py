"""크레딧 원장: 메시지 하나에 1개, 0 이면 402, 실패하면 되돌리고, 위기 표현은 0 이어도 답한다."""
import json
import uuid

from fastapi.testclient import TestClient

from app import agent, credits
from app.main import app, store

client = TestClient(app)
UID = "local-user"  # AUTH=none 의 개발용 사용자


def balance():
    return client.get("/api/credits").json()["balance"]


def set_balance(target):
    """원장을 고치지 않고, 차이만큼 줄을 하나 더 쌓아 잔액을 맞춘다(시험용)."""
    diff = target - balance()
    if diff:
        store.add_credit(UID, diff, "test", f"test:{uuid.uuid4()}")


def test_message_spends_one_credit():
    before = balance()
    r = client.post("/api/chat", json={"message": "안녕"})
    assert r.status_code == 200
    first = json.loads(r.text.splitlines()[0])
    assert first == {"type": "credits", "balance": before - 1}
    assert balance() == before - 1
    latest = client.get("/api/credits").json()["history"][0]
    assert (latest["delta"], latest["reason"]) == (-1, "chat")


def test_zero_balance_is_rejected_with_402():
    keep = balance()
    set_balance(0)
    try:
        r = client.post("/api/chat", json={"message": "안녕"})
        assert r.status_code == 402
        assert r.json() == {"error": "insufficient_credits", "message": credits.INSUFFICIENT_MESSAGE, "balance": 0}
        assert balance() == 0  # 거절된 요청은 아무것도 쌓지 않는다
    finally:
        set_balance(keep)


def test_crisis_message_is_answered_even_with_zero_credits():
    keep = balance()
    set_balance(0)
    try:
        r = client.post("/api/chat", json={"message": "요즘 너무 죽고 싶어"})
        assert r.status_code == 200
        assert "109" in "".join(json.loads(line).get("text", "") for line in r.text.splitlines())
        assert balance() == 0  # 차감하지도 않는다
    finally:
        set_balance(keep)


def test_failed_reply_gets_refund_line(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("LLM 이 응답하지 않음")
        yield  # 제너레이터로 만든다

    monkeypatch.setattr(agent, "run_turn", broken)
    before = balance()
    r = client.post("/api/chat", json={"message": "안녕"})
    assert any(json.loads(line)["type"] == "error" for line in r.text.splitlines())
    assert balance() == before  # -1 다음에 +1 이 쌓였다
    reasons = [row["reason"] for row in client.get("/api/credits").json()["history"][:2]]
    assert reasons == ["refund", "chat"]


def test_signup_credits_are_given_only_once():
    uid = f"new-{uuid.uuid4()}"
    credits.ensure_signup(store, uid)
    credits.ensure_signup(store, uid)  # 같은 ref(signup:사용자ID)라 두 번째는 쌓이지 않는다
    assert store.credit_balance(uid) == credits.signup_credits() == 30


def test_spend_never_goes_below_zero():
    uid = f"new-{uuid.uuid4()}"
    store.add_credit(uid, 2, "test", f"test:{uuid.uuid4()}")
    results = [credits.spend(store, uid)[0] for _ in range(5)]
    assert results == [True, True, False, False, False]
    assert store.credit_balance(uid) == 0
