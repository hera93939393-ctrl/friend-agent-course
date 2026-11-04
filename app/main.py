"""AI 친구 에이전트 서비스.

화면(/)과 API를 FastAPI 하나가 함께 제공한다.
- /health, /version: 서비스 상태와 배포 버전
- /api/config: 화면이 쓰는 설정(로그인 방식, 개발용 패널 표시 여부)
- /api/character, /api/settings: 캐릭터와 사용자 설정
- /api/chat: 메시지 한 턴. 결과를 이벤트(JSON 한 줄씩)로 스트리밍한다
- /api/greeting: 앱을 열 때 먼저 말 걸기
- /api/ocr: 사진 속 글자 읽기(OCR_ENABLED=true 일 때)
- /api/memories, /api/reminders, /api/data: 기억, 약속, 내 데이터 관리
"""
import html
import json
import os
import time

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import agent, credits, ocr, payments, tracing, when
from app.auth import Auth, User
from app.safety import is_crisis
from app.config import ROOT, load_settings
from app.llm import make_llm
from app.logs import log
from app.persona import DEFAULT_CHARACTER, OPTIONS, build_persona, clean_character
from app.store.base import make_store
from app.tools import CITIES

settings = load_settings()  # 꼭 필요한 값이 없으면 여기서 종료한다
llm = make_llm(settings)
store = make_store(settings)
current_user = Auth(settings)

app = FastAPI(title="AI 친구 에이전트")

# 시작 지연 스위치(배포 실습용). 포트는 바로 열지만 정한 시간 동안은 준비 중이라 모든 요청에 503을 돌려준다.
# AI 서비스가 시작할 때 모델 설정이나 색인을 불러오느라 늦게 준비되는 상황을 흉내 낸다. 기본값 0(끔).
SLOW_START_SECONDS = float(os.environ.get("SLOW_START_SECONDS", "0") or 0)
READY_AT = time.time() + SLOW_START_SECONDS
if SLOW_START_SECONDS:
    log("WARNING", f"시작 지연 스위치가 켜져 있습니다. {SLOW_START_SECONDS:.0f}초 동안 준비 중으로 응답합니다.")


@app.middleware("http")
async def not_ready_yet(request, call_next):
    if time.time() < READY_AT:
        return JSONResponse({"status": "starting"}, status_code=503)
    return await call_next(request)


def get_llm():
    """테스트에서는 이 함수를 가짜 LLM을 돌려주는 함수로 바꿔 끼운다."""
    return llm


class ChatRequest(BaseModel):
    message: str


class SettingsRequest(BaseModel):
    location: str | None = None
    timezone: str | None = None


class ReportRequest(BaseModel):
    message_id: int
    reason: str = ""


class FeedbackRequest(BaseModel):
    message_id: int
    value: Literal["up", "down"]


@app.get("/", response_class=HTMLResponse)
def home():
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    banner = "" if settings.env in ("production", "local") else f'<div class="banner">테스트 환경 ({settings.env})</div>'
    if os.environ.get("BANNER"):  # 이미지를 새로 만들지 않고 환경변수만 바꿔 새 리비전을 만들 때 쓰는 화면 문구
        banner += f'<div class="banner">{html.escape(os.environ["BANNER"][:80])}</div>'
    return page.replace("<!--ENV_BANNER-->", banner)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"commit": settings.commit, "revision": settings.revision, "env": settings.env,
            "llm": settings.provider, "model": settings.model, "storage": settings.storage, "auth": settings.auth,
            "banner": os.environ.get("BANNER", "")}


@app.get("/api/config")
def config():
    # publishable key 는 브라우저에 공개해도 되는 키다. secret key 는 절대 여기에 넣지 않는다.
    return {"env": settings.env, "auth": settings.auth, "storage": settings.storage,
            "supabase_url": settings.supabase_url if settings.auth == "supabase" else "",
            "supabase_publishable_key": settings.supabase_publishable_key if settings.auth == "supabase" else "",
            "google_login": settings.google_login, "show_turn_panel": settings.show_turn_panel,
            "ocr_enabled": settings.ocr_enabled,
            "toss_client_key": settings.toss_client_key,  # 클라이언트 키만. 시크릿 키는 여기에 넣지 않는다
            "options": OPTIONS, "cities": list(CITIES)}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "anonymous": user.anonymous, "email": user.email,
            "has_character": store.get_character(user.id) is not None}


# ── 캐릭터와 설정 ──────────────────────────────────────────────────

@app.get("/api/character")
def get_character(user: User = Depends(current_user)):
    c = store.get_character(user.id)
    return {"character": c or DEFAULT_CHARACTER, "saved": c is not None}


@app.put("/api/character")
def put_character(data: dict, user: User = Depends(current_user)):
    c = clean_character(data)
    store.save_character(user.id, c)
    log("INFO", "캐릭터 저장")
    return {"character": c}


@app.post("/api/character/preview")
def preview_character(data: dict):
    return {"prompt": build_persona(clean_character(data))}


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user)):
    return store.get_settings(user.id)


@app.put("/api/settings")
def put_settings(req: SettingsRequest, user: User = Depends(current_user)):
    changes = {}
    if req.location in CITIES:
        changes["location"] = req.location
    if req.timezone:
        changes["timezone"] = req.timezone if req.timezone == "Asia/Seoul" else "Asia/Seoul"  # 지금은 한국 시간만
    store.save_settings(user.id, changes)
    return store.get_settings(user.id)


# ── 대화 ───────────────────────────────────────────────────────────

@app.get("/api/messages")
def messages(user: User = Depends(current_user)):
    show = settings.show_turn_panel
    return [{"id": m["id"], "role": m["role"], "content": m["content"],
             "meta": m["meta"] if show else {}, "created_at": m["created_at"].isoformat()}
            for m in store.recent_messages(user.id, 50)]


@app.post("/api/chat")
def chat(req: ChatRequest, user: User = Depends(current_user), model=Depends(get_llm)):
    message = req.message.strip()[:1000]
    if not message:
        raise HTTPException(400, "메시지가 비어 있습니다")

    # 크레딧: 답을 만들기 전에 서버에서 뺀다. 402 는 스트리밍이 시작되기 전에만 보낼 수 있다.
    # 위기 표현은 크레딧이 0 이어도 막지 않고, 차감하지도 않는다.
    spent_ref, balance = None, None
    if not is_crisis(message):
        credits.ensure_signup(store, user)
        ok, balance, spent_ref = credits.spend(store, user.id)
        if not ok:
            log("INFO", "크레딧 부족으로 거절", balance=balance)
            return credits.insufficient(balance)

    def events():
        if balance is not None:
            yield json.dumps({"type": "credits", "balance": balance}) + "\n"
        try:
            for event in agent.run_turn(message, user.id, model, store, settings.show_turn_panel):
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        except Exception as error:
            log("ERROR", "대화 처리 실패", error=type(error).__name__)
            if spent_ref:  # 답을 못 만들었으면 돈을 받지 않는다: 지우지 않고 +1 줄을 쌓는다
                credits.refund(store, user.id, spent_ref)
                yield json.dumps({"type": "credits", "balance": balance + 1}) + "\n"
            yield json.dumps({"type": "error", "message": "답을 만들지 못했어요. 잠시 뒤 다시 보내 주세요."}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.get("/api/credits")
def get_credits(user: User = Depends(current_user)):
    """남은 크레딧과 최근 원장 10줄. 화면은 이 값을 보여 주기만 하고, 계산은 서버가 한다."""
    return credits.summary(store, user)


# ── 크레딧 결제(app/payments.py) ───────────────────────────────────

class OrderRequest(BaseModel):
    product_id: str  # 화면은 상품 ID 만 보낸다. 금액은 서버 상품표에서 찾는다


class ConfirmRequest(BaseModel):
    paymentKey: str
    orderId: str
    amount: int | str  # 비교만 하고, 승인에는 주문에 저장한 금액을 쓴다


class FailRequest(BaseModel):
    code: str = ""


def payments_on():
    if not (settings.toss_client_key and settings.toss_secret_key):
        raise HTTPException(503, {"error": "payments_off", "message": "지금은 결제를 받을 수 없어요."})


@app.get("/api/products")
def products():
    return {"products": credits.product_list(), "enabled": bool(settings.toss_client_key and settings.toss_secret_key)}


@app.post("/api/orders")
def create_order(req: OrderRequest, user: User = Depends(current_user)):
    payments_on()
    return payments.create(store, user.id, req.product_id)


@app.post("/api/payments/confirm")
def confirm_payment(req: ConfirmRequest, user: User = Depends(current_user)):
    payments_on()
    return payments.confirm(store, settings.toss_secret_key, user.id, req.paymentKey, req.orderId, req.amount)


@app.post("/api/orders/{order_id}/fail")
def fail_order(order_id: str, req: FailRequest, user: User = Depends(current_user)):
    return payments.mark_failed(store, user.id, order_id, req.code)


@app.get("/pay/success", response_class=HTMLResponse)
@app.get("/pay/fail", response_class=HTMLResponse)
def pay_return():
    """결제 뒤 돌아오는 주소. 로그인을 다시 쓰려고 같은 화면을 보여 주고, 화면이 주소를 보고 승인·실패를 처리한다."""
    return home()


@app.post("/api/greeting")
def greeting(user: User = Depends(current_user), model=Depends(get_llm)):
    return {"greeting": agent.greet(user.id, model, store)}


@app.post("/api/ocr")
async def read_photo(photo: UploadFile, user: User = Depends(current_user)):
    if not settings.ocr_enabled:
        raise HTTPException(404, "사진 글자 읽기가 꺼져 있습니다")
    try:
        return {"text": ocr.read_text(await photo.read())}
    except ocr.OcrError as error:
        raise HTTPException(500, str(error)) from error


def my_reply(user, message_id):
    """로그인한 사용자의 답 메시지만 돌려준다. 남의 메시지이거나 사용자 메시지면 404."""
    message = store.get_message(user.id, message_id)
    if not message or message["role"] != "assistant":
        raise HTTPException(404, "메시지를 찾지 못했습니다.")
    return message


def send_score(message, name, value, comment=None):
    """Langfuse 점수로 보낸다. 키가 없거나 보내기에 실패해도 사용자 요청은 성공으로 끝낸다."""
    try:
        return tracing.score(message["meta"].get("trace_id"), name, value, comment)
    except Exception as error:
        log("WARNING", "Langfuse 점수 보내기 실패", name=name, error=type(error).__name__)
        return False


@app.post("/api/feedback")
def feedback(req: FeedbackRequest, user: User = Depends(current_user)):
    """답 옆의 좋아요(1), 별로예요(0)를 user-feedback 점수로 보낸다."""
    message = my_reply(user, req.message_id)
    sent = send_score(message, "user-feedback", 1 if req.value == "up" else 0)
    log("INFO", "답변 반응", message_id=req.message_id, value=req.value, scored=sent)
    return {"ok": True}


@app.post("/api/report")
def report(req: ReportRequest, user: User = Depends(current_user)):
    """부적절한 답 신고. 로그에는 사유의 길이만 남기고, 사유 원문은 Langfuse 점수의 comment 로만 보낸다."""
    message = my_reply(user, req.message_id)
    sent = send_score(message, "report", 1, req.reason[:500])
    log("WARNING", "부적절한 답변 신고", message_id=req.message_id, reason_length=len(req.reason), scored=sent)
    return {"ok": True}


# ── 기억, 약속, 내 데이터 ───────────────────────────────────────────

@app.get("/api/memories")
def list_memories(user: User = Depends(current_user)):
    return [{"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in store.list_memories(user.id)]


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(current_user)):
    store.delete_memory(user.id, memory_id)
    return {"ok": True}


@app.delete("/api/memories")
def clear_memories(user: User = Depends(current_user)):
    store.clear_memories(user.id)
    return {"ok": True}


@app.get("/api/reminders")
def list_reminders(user: User = Depends(current_user)):
    tz = when.now_in(store.get_settings(user.id)["timezone"]).tzinfo
    return [{"id": r["id"], "text": r["text"], "status": r["status"],
             "due_label": when.label(r["due_at"].astimezone(tz))} for r in store.list_reminders(user.id)]


@app.delete("/api/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int, user: User = Depends(current_user)):
    store.cancel_reminder(user.id, reminder_id)
    return {"ok": True}


@app.delete("/api/data")
def delete_data(user: User = Depends(current_user)):
    store.delete_all(user.id)
    log("INFO", "사용자 데이터 전체 삭제")
    return {"ok": True}
