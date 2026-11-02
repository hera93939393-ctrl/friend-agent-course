"""Langfuse 계측(트레이스)을 한곳에 모아 둔다.

LANGFUSE_PUBLIC_KEY 와 LANGFUSE_SECRET_KEY 가 둘 다 있을 때만 Langfuse로 보낸다.
키가 없으면(테스트, CI, 키를 넣지 않은 동료의 컴퓨터) 아무것도 보내지 않는 빈 기록기를 써서 앱은 그대로 돈다.

단계는 '지금 단계'를 자동으로 찾지 않고 직접 만들어 직접 닫는다(start_observation → end).
답을 스트리밍하는 동안 FastAPI가 여러 스레드에서 이어 받으므로, 자동으로 찾으면 엉뚱한 단계에 붙을 수 있다.

대화와 기억 원문은 남긴다(원문이 없으면 엉뚱한 답의 원인을 읽을 수 없다).
대신 보내기 전에 전화번호, 이메일, 카드 번호를 가린다. 서버 로그(log)에는 여전히 원문을 남기지 않는다.
SDK 근거: https://langfuse.com/docs/observability/sdk/python/instrumentation , langfuse==5.0.4
"""
import os
import re
from datetime import datetime, timezone

# 카드 번호를 전화번호보다 먼저 가린다(16자리 안에 전화번호 모양이 들어 있을 수 있어서).
PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[이메일]"),
    (re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b"), "[카드번호]"),
    (re.compile(r"\b01[016789][ -]?\d{3,4}[ -]?\d{4}\b"), "[전화번호]"),
    (re.compile(r"\b0\d{1,2}-\d{3,4}-\d{4}\b"), "[전화번호]"),
]


def mask_text(text):
    for pattern, label in PATTERNS:
        text = pattern.sub(label, text)
    return text


def mask(*, data, **kwargs):
    """Langfuse SDK 가 보내기 직전에 부른다. 글자 안의 개인정보 모양만 바꾸고 구조는 그대로 둔다."""
    if isinstance(data, str):
        return mask_text(data)
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=v) for v in data]
    return data


class _Off:
    """키가 없을 때 쓰는 빈 기록기. 무엇을 불러도 아무 일도 하지 않는다."""

    def start_observation(self, **kwargs):
        return self

    def update(self, **kwargs):
        return self

    def end(self, **kwargs):
        return self


OFF = _Off()
_client = None


def client():
    """Langfuse 클라이언트. 키가 없으면 None."""
    global _client
    if _client is None:
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        if not (public_key and secret_key):
            return None
        from langfuse import Langfuse

        _client = Langfuse(public_key=public_key, secret_key=secret_key,
                           base_url=os.environ.get("LANGFUSE_BASE_URL") or None, mask=mask)
    return _client


def start_trace(name, as_type="span", **fields):
    """트레이스의 맨 위 단계(chat-turn, greeting)를 만든다. 다 쓰면 end() 를 불러야 보내진다."""
    langfuse = client()
    if langfuse is None:
        return OFF
    return langfuse.start_observation(name=name, as_type=as_type, **fields)


def usage_details(usage):
    """LLM 클래스가 채운 usage dict 를 Langfuse 형식으로. 모르면 None(가짜 LLM, 규칙)."""
    if usage and usage.get("input") is not None:
        return {"input": usage["input"], "output": usage.get("output") or 0}
    return None


def now():
    return datetime.now(timezone.utc)
