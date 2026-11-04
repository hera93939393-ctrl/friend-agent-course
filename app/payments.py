"""크레딧 결제(토스페이먼츠 결제위젯 v2). 크레딧을 늘리는 곳은 이 파일의 confirm() 한 곳뿐이다.

흐름: 화면이 상품 ID 로 주문을 만든다(create) → 결제위젯으로 결제 → 성공 주소로 돌아온 값을 서버로 보낸다
→ confirm(): 주문 확인 → 토스페이먼츠 승인 API → 주문 완료 + 원장 +크레딧(ref order:주문번호).

- 금액과 크레딧 수는 화면이 보낸 값이 아니라 주문에 저장한 값(= 서버 상품표에서 찾은 값)만 쓴다.
- 같은 주문을 두 번 승인해도 한 번만 지급된다: 주문은 pending 일 때만 done 으로 바뀌고, 원장 ref 가 유일하다.
- 결과를 모를 때(네트워크 오류)는 실패로 적지 않고 pending 으로 둔다. 돈이 나갔는데 실패로 적는 일을 막으려고.
- 시크릿 키는 로그·응답에 넣지 않는다. paymentKey 는 로그에 앞 8자만 남긴다.

토스페이먼츠 쪽 규칙의 근거: 공식 샘플 tosspayments-sample/express-javascript(server.js, widget/*.html),
API 레퍼런스 https://docs.tosspayments.com/reference (결제 승인 POST /v1/payments/confirm, Basic 인증, status DONE)
"""
import base64
import uuid
from datetime import datetime

import httpx
from fastapi import HTTPException

from app import credits
from app.logs import log

CONFIRM_URL = "https://api.tosspayments.com/v1/payments/confirm"


def short(payment_key):
    return (payment_key or "")[:8] + "…"


def fail(status, error, message):
    raise HTTPException(status, {"error": error, "message": message})


def create(store, uid, product_id):
    """상품 ID 로 주문을 대기 상태로 저장한다. 금액과 크레딧은 상품표에서 찾는다."""
    product = credits.PRODUCTS.get(product_id)
    if not product:
        fail(400, "unknown_product", "없는 상품이에요.")
    order_id = f"order_{uuid.uuid4().hex}"  # 추측할 수 없는 무작위 값. 토스 규칙(영문·숫자·-_=, 6~64자)에 맞다
    order = store.create_order(uid, order_id, product_id, product["amount"], product["credits"])
    log("INFO", "주문 생성", order_id=order_id, product_id=product_id, amount=product["amount"])
    return {"order_id": order_id, "amount": order["amount"], "credits": order["credits"], "order_name": product["name"]}


def toss_confirm(secret_key, payment_key, order_id, amount):
    """토스페이먼츠 결제 승인 API. (HTTP 상태 코드, 응답 JSON)"""
    auth = base64.b64encode(f"{secret_key}:".encode()).decode()  # 시크릿 키 뒤 콜론을 빠뜨리지 않는다
    r = httpx.post(CONFIRM_URL, timeout=15, headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
                   json={"paymentKey": payment_key, "orderId": order_id, "amount": amount})
    return r.status_code, r.json()


def _grant(store, uid, order):
    """주문의 크레딧을 원장에 쌓는다. ref 가 유일해서 몇 번 불러도 한 번만 쌓인다."""
    store.add_credit(uid, order["credits"], "purchase", f"order:{order['order_id']}")
    return {"ok": True, "order_id": order["order_id"], "credits": order["credits"], "balance": store.credit_balance(uid)}


def confirm(store, secret_key, uid, payment_key, order_id, amount):
    order = store.get_order(uid, order_id)
    if not order:
        fail(404, "order_not_found", "주문을 찾을 수 없어요.")
    if order["status"] == "done":  # 같은 결제를 다시 보낸 경우: 토스를 다시 부르지 않고, 지급도 다시 하지 않는다
        return {**_grant(store, uid, order), "already": True}
    if order["status"] == "failed":
        fail(409, "order_failed", "이미 실패한 주문이에요. 다시 결제해 주세요.")
    try:
        sent_amount = int(amount)
    except (TypeError, ValueError):
        sent_amount = None
    if sent_amount != order["amount"]:  # 금액이 바뀐 요청: 토스를 부르지 않고 거절한다
        log("WARNING", "결제 금액 불일치로 거절", order_id=order_id, expected=order["amount"], got=str(amount)[:20])
        fail(400, "amount_mismatch", "결제 금액이 주문과 달라요.")

    try:
        status, body = toss_confirm(secret_key, payment_key, order_id, order["amount"])
    except (httpx.HTTPError, ValueError) as error:  # 결과를 모름: pending 으로 둔다
        log("ERROR", "결제 승인 요청 실패", order_id=order_id, payment_key=short(payment_key), error=type(error).__name__)
        fail(502, "confirm_unknown", "결제 확인이 늦어지고 있어요. 잠시 뒤 다시 확인해 주세요.")

    if status != 200:
        code = str(body.get("code", "UNKNOWN"))[:64]
        log("WARNING", "결제 승인 거절", order_id=order_id, payment_key=short(payment_key), code=code)
        store.finish_order(uid, order_id, "failed", fail_code=code)
        fail(400, "confirm_failed", body.get("message") or "결제를 승인하지 못했어요.")

    if body.get("status") != "DONE" or body.get("totalAmount") != order["amount"] or body.get("orderId") != order_id:
        # 토스는 승인했다는데 내용이 주문과 다르다: 지급하지 않고 사람이 확인하게 pending 으로 둔다
        log("ERROR", "승인 응답이 주문과 다름", order_id=order_id, status=body.get("status"), total=body.get("totalAmount"))
        fail(502, "confirm_mismatch", "결제 내용을 확인하지 못했어요. 문의해 주세요.")

    approved_at = datetime.fromisoformat(body["approvedAt"]) if body.get("approvedAt") else None
    store.finish_order(uid, order_id, "done", payment_key=payment_key, approved_at=approved_at)
    log("INFO", "결제 승인", order_id=order_id, payment_key=short(payment_key), amount=order["amount"], credits=order["credits"])
    return _grant(store, uid, store.get_order(uid, order_id))


def mark_failed(store, uid, order_id, code):
    """실패 주소로 돌아왔을 때. 내 pending 주문만 failed 로 바꾼다."""
    changed = store.finish_order(uid, order_id, "failed", fail_code=str(code or "UNKNOWN")[:64])
    log("INFO", "결제 실패 기록", order_id=order_id, code=str(code)[:64], changed=bool(changed))
    return {"ok": True}
