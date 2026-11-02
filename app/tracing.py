"""Langfuse 계측(트레이스)을 한곳에 모아 둔다.

LANGFUSE_PUBLIC_KEY 와 LANGFUSE_SECRET_KEY 가 둘 다 있을 때만 Langfuse로 보낸다.
키가 없으면(테스트, CI, 키를 넣지 않은 동료의 컴퓨터) 아무것도 보내지 않는 빈 기록기를 써서 앱은 그대로 돈다.

단계는 '지금 단계'를 자동으로 찾지 않고 직접 만들어 직접 닫는다(start_observation → end).
답을 스트리밍하는 동안 FastAPI가 여러 스레드에서 이어 받으므로, 자동으로 찾으면 엉뚱한 단계에 붙을 수 있다.

대화와 기억 원문은 남긴다(원문이 없으면 엉뚱한 답의 원인을 읽을 수 없다).
대신 보내기 전에 전화번호, 이메일, 카드 번호를 가린다. 서버 로그(log)에는 여전히 원문을 남기지 않는다.

속성(사용자, 세션, 환경, 버전, 꼬리표)은 propagate_attributes 로 붙인다. 한 턴 전체를 with 로 감싸면
스트리밍 도중 스레드가 바뀔 때 속성이 풀리거나 다른 요청에 묻으므로, 단계를 만드는 그 순간에만 감싼다(Observation).
SDK 근거: https://langfuse.com/docs/observability/sdk/python/instrumentation , langfuse==5.0.4
"""
import os
import re
from contextlib import nullcontext
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

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

    def with_attrs(self, **kwargs):
        return self

    trace_id = None

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


def _propagate(attrs):
    if not attrs:
        return nullcontext()
    from langfuse import propagate_attributes

    return propagate_attributes(**attrs)


class Observation:
    """Langfuse 단계 하나와, 그 아래 단계에 붙일 속성을 같이 들고 다닌다."""

    def __init__(self, observation, attrs):
        self._observation = observation
        self.attrs = attrs

    @property
    def trace_id(self):
        return getattr(self._observation, "trace_id", None)

    def start_observation(self, **fields):
        with _propagate(self.attrs):
            return Observation(self._observation.start_observation(**fields), self.attrs)

    def with_attrs(self, metadata=None, tags=None):
        """같은 단계에 속성을 더한 것. 이 뒤에 만드는 단계부터 붙는다(이미 만든 단계에는 붙지 않는다)."""
        attrs = dict(self.attrs or {})
        if metadata:
            attrs["metadata"] = {**attrs.get("metadata", {}), **metadata}
        if tags:
            attrs["tags"] = sorted(set(attrs.get("tags", [])) | set(tags))
        return Observation(self._observation, attrs)

    def update(self, **fields):
        self._observation.update(**fields)
        return self

    def end(self, **fields):
        self._observation.end(**fields)
        return self


def turn_attrs(uid):
    """한 턴(또는 먼저 말 걸기)에 붙일 속성. 세션은 '사용자 ID + 한국 날짜' — 하루치 대화를 한 세션으로 본다."""
    return {
        "user_id": uid,
        "session_id": f"{uid}-{datetime.now(KST):%Y-%m-%d}",
        "environment": os.environ.get("ENV", "local").strip() or "local",
        "version": os.environ.get("K_REVISION", "local"),
        "metadata": {"commit": os.environ.get("COMMIT_SHA", "local").strip()},
    }


def start_trace(name, as_type="span", attrs=None, **fields):
    """트레이스의 맨 위 단계(chat-turn, greeting)를 만든다. 다 쓰면 end() 를 불러야 보내진다."""
    langfuse = client()
    if langfuse is None:
        return OFF
    with _propagate(attrs):
        return Observation(langfuse.start_observation(name=name, as_type=as_type, **fields), attrs)


def score(trace_id, name, value, comment=None):
    """사용자 반응을 트레이스에 BOOLEAN 점수로 붙인다(1=참, 0=거짓). 키나 트레이스 ID가 없으면 건너뛰고 False.

    근거: https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk (create_score)
    """
    langfuse = client()
    if langfuse is None or not trace_id:
        return False
    # 점수도 트레이스와 같은 environment 로 둔다(안 넣으면 default 로 쌓여 staging 으로 거르면 점수가 빠진다)
    langfuse.create_score(trace_id=trace_id, name=name, value=value, data_type="BOOLEAN", comment=comment or None,
                          environment=os.environ.get("ENV", "local").strip() or "local")
    return True


def usage_details(usage):
    """LLM 클래스가 채운 usage dict 를 Langfuse 형식으로. 모르면 None(가짜 LLM, 규칙)."""
    if usage and usage.get("input") is not None:
        return {"input": usage["input"], "output": usage.get("output") or 0}
    return None


def now():
    return datetime.now(timezone.utc)
