"""테스트는 실제 AI를 부르지 않는다. 가짜 응답(fake)과 서버 메모리 저장소로 실행한다.

실제 AI 호출은 비용이 들고, 느리고, 같은 질문에도 답이 달라서 통과와 실패를 판정할 수 없다.
"""
import os

os.environ.update({"LLM_PROVIDER": "fake", "LLM_API_KEY": "", "STORAGE": "memory", "AUTH": "none"})
# Langfuse 키를 비워 둔다. 비우지 않으면 내 .env 의 키를 읽어 테스트 대화가 진짜 Langfuse 프로젝트로 올라간다.
os.environ.update({"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""})
# 결제 키도 가짜 값으로 덮는다. 테스트는 토스페이먼츠를 부르지 않는다(tests/test_payments.py 가 승인 API 를 가짜로 바꾼다).
os.environ.update({"TOSS_CLIENT_KEY": "test_ck_fake_for_tests", "TOSS_SECRET_KEY": "test_sk_fake_for_tests"})
os.environ.setdefault("ENV", "test")
