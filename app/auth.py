"""요청을 보낸 사용자 확인하기.

- AUTH=none: 로그인 없이 모든 요청을 개발용 사용자 한 명으로 본다. 노트북과 첫 배포용이다.
- AUTH=guest: 화면이 브라우저마다 임의의 손님 번호를 만들어 X-Guest-Id 헤더로 보내고, 서버는 번호별로 사용자를 나눈다.
  번호를 확인할 방법이 없어 남의 번호를 알면 그 사람 데이터를 볼 수 있다. 여러 사람이 써 보는 연습용이며,
  실제 서비스는 AUTH=supabase 를 쓴다.
- AUTH=supabase: 화면이 Supabase Auth로 로그인(익명 또는 구글)하고 받은 access token을 요청마다 보낸다.
  서버는 그 토큰을 Supabase Auth에 물어 확인하고 user_id 를 얻는다. 확인한 user_id 만 저장소 조회에 쓴다.
"""
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request

from app.logs import log

LOCAL_USER = "local-user"
CACHE_SECONDS = 300
GUEST_ID = re.compile(r"^[A-Za-z0-9-]{16,64}$")


@dataclass
class User:
    id: str
    anonymous: bool
    email: str | None = None


class Auth:
    def __init__(self, settings):
        self.mode = settings.auth
        self.url = settings.supabase_url
        self.key = settings.supabase_publishable_key
        self.cache = {}  # token → (만료 시각, User)

    def __call__(self, request: Request) -> User:
        if self.mode == "none":
            return User(LOCAL_USER, anonymous=True)
        if self.mode == "guest":
            guest = request.headers.get("x-guest-id", "").strip()
            if not GUEST_ID.match(guest):
                raise HTTPException(400, "손님 번호가 없거나 올바르지 않습니다")
            return User(f"guest-{guest}", anonymous=True)
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(401, "로그인이 필요합니다")
        return self.verify(header[7:].strip())

    def verify(self, token):
        hit = self.cache.get(token)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            r = httpx.get(f"{self.url}/auth/v1/user", timeout=5,
                          headers={"apikey": self.key, "Authorization": f"Bearer {token}"})
        except httpx.HTTPError as error:
            log("ERROR", "로그인 확인 요청 실패", error=type(error).__name__)
            raise HTTPException(503, "로그인을 확인하지 못했습니다") from error
        if r.status_code != 200:
            raise HTTPException(401, "로그인이 만료되었거나 올바르지 않습니다")
        data = r.json()
        user = User(data["id"], anonymous=bool(data.get("is_anonymous")), email=data.get("email") or None)
        if len(self.cache) > 1000:
            self.cache.clear()
        self.cache[token] = (time.time() + CACHE_SECONDS, user)
        return user
