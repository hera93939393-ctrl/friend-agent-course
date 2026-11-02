"""Langfuse 계측: 키가 없으면 꺼지는지, 개인정보를 가리는지, 단계 이름이 계측 구조 표대로 남는지 본다.

진짜 Langfuse로는 보내지 않는다. 단계를 받아 적기만 하는 가짜 기록기로 바꿔 끼운다.
"""
import json

from fastapi.testclient import TestClient

from app import tracing
from app.main import app

client = TestClient(app)


class Recorder:
    """Langfuse 단계 대신 이름과 값을 받아 적는다."""

    def __init__(self, **fields):
        self.fields = fields
        self.children = []
        self.ended = False

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
    traces = []

    def start_trace(name, as_type="span", **fields):
        traces.append(Recorder(name=name, as_type=as_type, **fields))
        return traces[-1]

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
