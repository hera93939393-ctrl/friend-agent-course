"""AUTH=guest: 손님 번호마다 사용자가 나뉘는지 본다."""
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import Auth


class _Settings:
    auth = "guest"
    supabase_url = ""
    supabase_publishable_key = ""


def _request(headers):
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "headers": raw})


def test_guest_ids_become_separate_users():
    auth = Auth(_Settings())
    a = auth(_request({"X-Guest-Id": "aaaaaaaa-1111-2222-3333-444444444444"}))
    b = auth(_request({"X-Guest-Id": "bbbbbbbb-1111-2222-3333-444444444444"}))
    assert a.id != b.id
    assert a.id == "guest-aaaaaaaa-1111-2222-3333-444444444444"


def test_missing_or_bad_guest_id_is_rejected():
    auth = Auth(_Settings())
    for headers in ({}, {"X-Guest-Id": "short"}, {"X-Guest-Id": "../../etc/passwd-aaaaaaaa"}):
        try:
            auth(_request(headers))
        except HTTPException as error:
            assert error.status_code == 400
        else:
            raise AssertionError(f"거절해야 하는 손님 번호: {headers}")
