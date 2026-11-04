"""Langfuse 계측: 키가 없으면 꺼지는지, 개인정보를 가리는지, 단계 이름이 계측 구조 표대로 남는지 본다.

진짜 Langfuse로는 보내지 않는다. 단계를 받아 적기만 하는 가짜 기록기로 바꿔 끼운다.
"""
import json
from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient

from app import tracing
from app.main import app, store

client = TestClient(app)


class Recorder:
    """Langfuse 단계 대신 이름과 값을 받아 적는다."""

    current_attrs = None  # 단계를 만드는 순간 감싸고 있던 속성(propagate_attributes 대신)

    def __init__(self, **fields):
        self.fields = fields
        self.children = []
        self.ended = False
        self.attrs = Recorder.current_attrs

    @property
    def name(self):
        return self.fields["name"]

    def start_observation(self, **fields):
        child = Recorder(**fields)
        self.children.append(child)
        return child

    def update(self, **fields):
        self.fields.update(fields)
        return self

    def end(self, **fields):
        self.ended = True
        return self


def record(monkeypatch):
    """진짜 Observation 포장은 그대로 쓰고, 그 안의 Langfuse 단계와 propagate_attributes 만 바꿔 끼운다."""
    traces = []

    @contextmanager
    def propagate(attrs):
        Recorder.current_attrs = attrs
        try:
            yield
        finally:
            Recorder.current_attrs = None

    def start_trace(name, as_type="span", attrs=None, **fields):
        with propagate(attrs):
            traces.append(Recorder(name=name, as_type=as_type, **fields))
        return tracing.Observation(traces[-1], attrs)

    monkeypatch.setattr(tracing, "_propagate", propagate)
    monkeypatch.setattr(tracing, "start_trace", start_trace)
    return traces


def test_tracing_is_off_without_keys():
    assert tracing.client() is None
    assert tracing.start_trace("chat-turn") is tracing.OFF


def test_mask_hides_phone_email_card_but_keeps_dates():
    text = "내 번호 010-1234-5678, 메일 kim.a@example.com, 카드 1234 5678 9012 3456. 약속은 2026-11-02 15시"
    masked = tracing.mask(data={"input": [text]})["input"][0]
    assert "010-1234-5678" not in masked and "[전화번호]" in masked
    assert "kim.a@example.com" not in masked and "[이메일]" in masked
    assert "1234 5678 9012 3456" not in masked and "[카드번호]" in masked
    assert "2026-11-02 15시" in masked


def test_chat_turn_records_steps_with_table_names(monkeypatch):
    traces = record(monkeypatch)
    r = client.post("/api/chat", json={"message": "내일 서울 날씨 어때?"})
    events = [json.loads(line) for line in r.text.splitlines()]

    turn = traces[-1]
    assert turn.name == "chat-turn" and turn.ended
    assert [c.name for c in turn.children] == ["safety", "choose-tools", "tool:get_weather", "reply", "memory-update"]
    assert all(c.ended for c in turn.children)

    steps = {c.name: c for c in turn.children}
    assert steps["choose-tools"].fields["as_type"] == "generation"
    assert steps["choose-tools"].fields["model"] == "fake"
    assert steps["choose-tools"].fields["metadata"]["decided_by"] == "rule"
    assert "ok" in steps["tool:get_weather"].fields["metadata"]
    assert steps["reply"].fields["model"] == "fake"
    assert steps["reply"].fields.get("completion_start_time") is not None
    assert turn.fields["output"] == steps["reply"].fields["output"]
    assert turn.fields["metadata"]["message_id"] == events[-1]["message_id"]


def test_attributes_follow_the_turn(monkeypatch):
    traces = record(monkeypatch)
    client.post("/api/chat", json={"message": "내일 서울 날씨 어때?"})
    turn = traces[-1]
    steps = {c.name: c for c in turn.children}

    today = datetime.now(tracing.KST).strftime("%Y-%m-%d")
    assert turn.attrs["user_id"] and "@" not in turn.attrs["user_id"]
    assert turn.attrs["session_id"] == f"{turn.attrs['user_id']}-{today}"
    assert turn.attrs["environment"] == "test" and turn.attrs["version"] == "local"
    assert turn.attrs["metadata"]["commit"]
    # 도구를 고르기 전 단계에는 도구 꼬리표가 없고, 고른 뒤 단계부터 붙는다
    assert "tags" not in (steps["choose-tools"].attrs or {})
    assert steps["reply"].attrs["tags"] == ["get_weather"]
    assert steps["reply"].attrs["metadata"]["tools"] == "get_weather"
    assert steps["reply"].attrs["metadata"]["safety"] == "false"


def last_reply_id():
    r = client.post("/api/chat", json={"message": "오늘 기분 좋아"})
    return [json.loads(line) for line in r.text.splitlines()][-1]["message_id"]


def test_feedback_and_report_become_scores(monkeypatch):
    sent = []
    monkeypatch.setattr(tracing, "score", lambda trace_id, name, value, comment=None: sent.append((name, value, comment)) or True)
    message_id = last_reply_id()
    assert client.post("/api/feedback", json={"message_id": message_id, "value": "up"}).json() == {"ok": True}
    assert client.post("/api/feedback", json={"message_id": message_id, "value": "down"}).json() == {"ok": True}
    assert client.post("/api/report", json={"message_id": message_id, "reason": "엉뚱한 답"}).json() == {"ok": True}
    assert sent == [("user-feedback", 1, None), ("user-feedback", 0, None), ("report", 1, "엉뚱한 답")]


def test_feedback_works_without_langfuse_keys():
    # 키가 없으면 점수는 건너뛰고 요청은 성공한다
    assert client.post("/api/feedback", json={"message_id": last_reply_id(), "value": "down"}).json() == {"ok": True}


def test_cannot_score_someone_elses_message():
    others = store.add_message("someone-else", "assistant", "남의 답", {"trace_id": "t-other"})
    assert client.post("/api/feedback", json={"message_id": others["id"], "value": "down"}).status_code == 404
    assert client.post("/api/report", json={"message_id": others["id"], "reason": ""}).status_code == 404
    assert client.post("/api/feedback", json={"message_id": last_reply_id(), "value": "meh"}).status_code == 422
