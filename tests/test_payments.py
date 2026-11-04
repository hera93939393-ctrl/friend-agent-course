"""크레딧 결제: 금액은 서버 상품표, 지급은 승인 확인 뒤 한 번만. 토스페이먼츠 승인 API 는 가짜로 바꿔 부른다."""
import threading

import httpx
import pytest
from fastapi.testclient import TestClient

from app import payments
from app.main import app, store

client = TestClient(app)
UID = "local-user"


class FakeToss:
    """토스페이먼츠 승인 API 대신. 몇 번 불렸는지 세고, 정해 둔 응답을 돌려준다."""
    def __init__(self, status=200, body=None, error=None):
        self.calls, self.status, self.body, self.error = [], status, body, error

    def __call__(self, secret_key, payment_key, order_id, amount):
        self.calls.append({"order_id": order_id, "amount": amount})
        if self.error:
            raise self.error
        body = self.body if self.body is not None else {
            "status": "DONE", "orderId": order_id, "totalAmount": amount, "approvedAt": "2026-11-04T10:00:00+09:00"}
        return self.status, body


@pytest.fixture
def toss(monkeypatch):
    fake = FakeToss()
    monkeypatch.setattr(payments, "toss_confirm", fake)
    return fake


def balance():
    return client.get("/api/credits").json()["balance"]


def new_order(product_id="credits_100"):
    r = client.post("/api/orders", json={"product_id": product_id})
    assert r.status_code == 200
    return r.json()


def confirm(order, amount=None, key="tpk_fake_payment_key_123"):
    return client.post("/api/payments/confirm", json={
        "paymentKey": key, "orderId": order["order_id"], "amount": order["amount"] if amount is None else amount})


def test_products_and_config_do_not_leak_secret():
    products = client.get("/api/products").json()
    assert [p["id"] for p in products["products"]] == ["credits_100", "credits_550"]
    config = client.get("/api/config").text
    assert "test_ck_fake_for_tests" in config          # 클라이언트 키는 화면에 보낸다
    assert "test_sk_fake_for_tests" not in config      # 시크릿 키는 보내지 않는다


def test_order_amount_comes_from_server_price_list():
    r = client.post("/api/orders", json={"product_id": "credits_550", "amount": 10, "credits": 99999})
    order = r.json()
    assert (order["amount"], order["credits"]) == (5000, 550)  # 화면이 보낸 금액·크레딧은 무시
    assert order["order_id"].startswith("order_") and len(order["order_id"]) == 38
    assert client.post("/api/orders", json={"product_id": "credits_free"}).status_code == 400


def test_confirm_grants_credits_once(toss):
    before = balance()
    order = new_order()
    r = confirm(order)
    assert r.status_code == 200 and r.json()["credits"] == 100
    assert balance() == before + 100
    saved = store.get_order(UID, order["order_id"])
    assert saved["status"] == "done" and saved["approved_at"] is not None
    assert toss.calls == [{"order_id": order["order_id"], "amount": 1000}]


def test_double_confirm_does_not_double_grant(toss):
    order = new_order()
    confirm(order)
    after_first = balance()
    r = confirm(order)  # 성공 주소를 새로고침하거나 요청을 다시 보낸 경우
    assert r.status_code == 200 and r.json()["already"] is True
    assert balance() == after_first
    assert len(toss.calls) == 1  # 토스를 다시 부르지 않는다


def test_concurrent_confirms_grant_once(toss):
    before = balance()
    order = new_order()
    results = []
    threads = [threading.Thread(target=lambda: results.append(confirm(order).status_code)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert balance() == before + 100
    rows = [r for r in store.ledger if r["ref"] == f"order:{order['order_id']}"]
    assert len(rows) == 1


def test_tampered_amount_is_rejected_without_calling_toss(toss):
    before = balance()
    order = new_order("credits_550")
    r = confirm(order, amount=100)  # 5000원 주문을 100원으로 바꿔 보낸 경우
    assert r.status_code == 400 and r.json()["detail"]["error"] == "amount_mismatch"
    assert toss.calls == []
    assert balance() == before
    assert store.get_order(UID, order["order_id"])["status"] == "pending"


def test_toss_rejection_marks_failed_and_grants_nothing(monkeypatch):
    monkeypatch.setattr(payments, "toss_confirm", FakeToss(400, {"code": "REJECT_CARD_PAYMENT", "message": "카드 거절"}))
    before = balance()
    order = new_order()
    r = confirm(order)
    assert r.status_code == 400
    saved = store.get_order(UID, order["order_id"])
    assert (saved["status"], saved["fail_code"]) == ("failed", "REJECT_CARD_PAYMENT")
    assert balance() == before


def test_network_error_keeps_order_pending(monkeypatch):
    monkeypatch.setattr(payments, "toss_confirm", FakeToss(error=httpx.ConnectTimeout("timeout")))
    before = balance()
    order = new_order()
    assert confirm(order).status_code == 502
    assert store.get_order(UID, order["order_id"])["status"] == "pending"  # 결과를 모르니 실패로 적지 않는다
    assert balance() == before


def test_mismatched_toss_response_grants_nothing(monkeypatch):
    before = balance()
    order = new_order()
    body = {"status": "DONE", "orderId": order["order_id"], "totalAmount": 1, "approvedAt": "2026-11-04T10:00:00+09:00"}
    monkeypatch.setattr(payments, "toss_confirm", FakeToss(200, body))
    assert confirm(order).status_code == 502
    assert balance() == before
    assert store.get_order(UID, order["order_id"])["status"] == "pending"


def test_other_users_order_is_not_found(toss):
    order = new_order()
    with pytest.raises(Exception) as caught:
        payments.confirm(store, "sk", "someone-else", "tpk_x", order["order_id"], order["amount"])
    assert caught.value.status_code == 404
    assert toss.calls == []


def test_fail_page_marks_only_pending_order(toss):
    pending = new_order()
    client.post(f"/api/orders/{pending['order_id']}/fail", json={"code": "PAY_PROCESS_CANCELED"})
    assert store.get_order(UID, pending["order_id"])["status"] == "failed"
    assert confirm(pending).status_code == 409  # 실패한 주문은 승인하지 않는다

    done = new_order()
    confirm(done)
    client.post(f"/api/orders/{done['order_id']}/fail", json={"code": "X"})
    assert store.get_order(UID, done["order_id"])["status"] == "done"  # 이미 끝난 주문은 바꾸지 못한다
