"""NATS request-reply 서비스 통합 검증 (ADR-018 전송로)."""

import asyncio
import json
import os

import nats
import pytest
from lf_ai_runtime.config import Config
from lf_ai_runtime.model import infer_subject
from lf_ai_runtime.service import serve

NATS_URL = os.environ.get("LF_TEST_NATS_URL", "nats://localhost:4222")


@pytest.fixture
async def ai_service():
    """rule 프로바이더 서비스를 in-process로 띄운다. NATS 미가용이면 skip (CI는 fail)."""
    try:
        probe = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    except Exception:
        if "LF_TEST_NATS_URL" in os.environ:
            raise
        pytest.skip(f"NATS 미가용 ({NATS_URL}) — infra/compose에서 nats를 켜라")

    env = "aitest"
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(Config(nats_url=NATS_URL, env=env, provider="rule"), stop=stop)
    )
    await asyncio.sleep(0.2)  # 구독 준비
    try:
        yield probe, env
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        await probe.close()


async def test_infer_roundtrip(ai_service):
    nc, env = ai_service
    request = {
        "task": "decide_action",
        "bundle": {"system": "당신은 김아리다.", "user": "행동을 결정하라.", "trace_id": "t-1"},
        "output_schema": {"type": "object"},
        "actor_tier": "warm",
        "trace": {"actor_id": "a_aria_kim", "tick": 1},
    }
    reply = await nc.request(
        infer_subject(env), json.dumps(request, ensure_ascii=False).encode(), timeout=5
    )
    response = json.loads(reply.data)
    assert response["ok"] is True
    assert response["output"]["action_kind"]
    assert response["model"] == "claude-haiku-4-5"  # warm 라우팅 (ADR-018 표)


async def test_malformed_request_returns_explicit_error(ai_service):
    nc, env = ai_service
    reply = await nc.request(infer_subject(env), b'{"task": "unknown_task"}', timeout=5)
    response = json.loads(reply.data)
    assert response["ok"] is False
    assert "task" in response["error"]
