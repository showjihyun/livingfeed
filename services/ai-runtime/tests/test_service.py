"""NATS request-reply 서비스 통합 검증 (ADR-018 전송로 + 유계 동시 처리)."""

import asyncio
import contextlib
import json
import os
import time

import nats
import pytest
from lf_ai_runtime.budget import BudgetGuard, MemoryStore
from lf_ai_runtime.config import Config
from lf_ai_runtime.model import Completion, infer_subject
from lf_ai_runtime.providers import ProviderError
from lf_ai_runtime.service import serve

NATS_URL = os.environ.get("LF_TEST_NATS_URL", "nats://localhost:4222")


def isolated_guard(env: str) -> BudgetGuard:
    """프로세스 안 카운터만 쓰는 가드 — 테스트가 상주 Redis를 건드리지 않게 한다.

    주입하지 않으면 serve()가 REDIS_URL(기본 localhost:6379/0)에 붙어 상주 세계의
    예산 카운터에 섞어 쓴다 — 테스트는 격리 표적만 겨눈다는 규약(conftest 가드)의
    연장이다.
    """
    return BudgetGuard(env, MemoryStore())


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
        serve(
            Config(nats_url=NATS_URL, env=env, provider="rule"),
            stop=stop,
            guard=isolated_guard(env),
        )
    )
    # 구독 준비를 폴링으로 기다린다 — 고정 sleep은 콜드 스타트(첫 import openai)에서
    # 레이스가 난다. 응답이 오면(파싱 실패라도) 구독이 선 것이다 (NoRespondersError 소멸)
    for _ in range(100):
        try:
            await probe.request(infer_subject(env), b"{}", timeout=0.2)
            break
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
            await asyncio.sleep(0.1)
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


# ─── 유계 동시 처리 ───────────────────────────────────────────────────────────
# 샤드 워커(ADR-012 Phase 2)가 병렬로 쏘는 요청을 겹쳐 처리한다.
# 요청별 task + Semaphore(LF_AI_CONCURRENCY) — 순서 보장 불필요(독립 reply subject).


class GaugeProvider:
    """느린 스텁 — 동시 in-flight를 계측한다. output_schema {"type": "object"} 전제."""

    name = "gauge"

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.inflight = 0
        self.max_inflight = 0
        self.completed = 0
        self.entered = asyncio.Event()  # 첫 요청이 처리에 들어선 순간

    async def complete(self, request, model, *, repair_errors=None, max_output_tokens=None):
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        self.entered.set()
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.inflight -= 1
        self.completed += 1
        return Completion(output={"echo": request.trace.get("n")})


class FailingProvider:
    name = "failing"

    async def complete(self, request, model, *, repair_errors=None, max_output_tokens=None):
        raise ProviderError("의도된 실패")


@contextlib.asynccontextmanager
async def serve_stub(env: str, provider, *, concurrency: int = 4):
    """스텁 프로바이더를 주입한 서비스를 in-process로 띄운다 (stop 이벤트 동봉)."""
    try:
        probe = await asyncio.wait_for(nats.connect(NATS_URL, connect_timeout=3), timeout=5)
    except Exception:
        if "LF_TEST_NATS_URL" in os.environ:
            raise
        pytest.skip(f"NATS 미가용 ({NATS_URL}) — infra/compose에서 nats를 켜라")

    stop = asyncio.Event()
    cfg = Config(nats_url=NATS_URL, env=env, provider="rule", concurrency=concurrency)
    task = asyncio.create_task(
        serve(cfg, stop=stop, providers={"rule": provider}, guard=isolated_guard(env))
    )
    # 구독 준비 폴링 — ai_service 픽스처와 동일한 이유 (고정 sleep은 레이스)
    for _ in range(100):
        try:
            await probe.request(infer_subject(env), b"{}", timeout=0.2)
            break
        except (nats.errors.NoRespondersError, nats.errors.TimeoutError):
            await asyncio.sleep(0.1)
    try:
        yield probe, stop, task
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=10)
        await probe.close()


def _request_bytes(n: int) -> bytes:
    return json.dumps(
        {
            "task": "decide_action",
            "bundle": {"system": "s", "user": "u", "trace_id": f"t-{n}"},
            "output_schema": {"type": "object"},
            "actor_tier": "warm",
            "trace": {"actor_id": "a", "tick": n, "n": n},
        },
        ensure_ascii=False,
    ).encode()


async def test_concurrent_requests_overlap():
    """느린 요청 2건이 겹쳐 처리된다 — 직렬 2×(1.5s)보다 확실히 빨리 끝난다."""
    provider = GaugeProvider(delay=0.75)
    async with serve_stub("aitest-overlap", provider) as (nc, _stop, _task):
        subject = infer_subject("aitest-overlap")
        started = time.monotonic()
        replies = await asyncio.gather(
            nc.request(subject, _request_bytes(1), timeout=5),
            nc.request(subject, _request_bytes(2), timeout=5),
        )
        elapsed = time.monotonic() - started
    responses = [json.loads(r.data) for r in replies]
    assert all(r["ok"] for r in responses)
    assert {r["output"]["echo"] for r in responses} == {1, 2}
    assert provider.max_inflight >= 2  # 실제로 겹쳤다 (시간 무관 증거)
    assert elapsed < 1.4  # 직렬이면 ≥ 1.5s — 여유 있는 상한


async def test_semaphore_caps_inflight():
    """동시 in-flight ≤ LF_AI_CONCURRENCY — 상한 초과도, 직렬 붕괴도 아니다."""
    provider = GaugeProvider(delay=0.15)
    async with serve_stub("aitest-cap", provider, concurrency=2) as (nc, _stop, _task):
        subject = infer_subject("aitest-cap")
        replies = await asyncio.gather(
            *(nc.request(subject, _request_bytes(n), timeout=10) for n in range(6))
        )
    assert all(json.loads(r.data)["ok"] for r in replies)
    assert provider.max_inflight == 2  # 상한까지 겹치되 넘지 않는다


async def test_provider_error_response_path_unchanged():
    """동시화 이후에도 실패는 명시적 오류 응답이다 — 조용한 유실 금지."""
    async with serve_stub("aitest-err", FailingProvider()) as (nc, _stop, _task):
        reply = await nc.request(infer_subject("aitest-err"), _request_bytes(1), timeout=5)
    response = json.loads(reply.data)
    assert response["ok"] is False
    assert "의도된 실패" in response["error"]


async def test_stop_drains_inflight_requests():
    """stop 시 진행 중 요청은 완주한다 — 응답이 유실되지 않는다."""
    provider = GaugeProvider(delay=1.0)
    async with serve_stub("aitest-drain", provider) as (nc, stop, serve_task):
        pending = asyncio.create_task(
            nc.request(infer_subject("aitest-drain"), _request_bytes(7), timeout=10)
        )
        await asyncio.wait_for(provider.entered.wait(), timeout=5)  # 처리 중일 때 멈춘다
        stop.set()
        await asyncio.wait_for(serve_task, timeout=10)
        assert provider.completed == 1  # 종료가 처리 완료를 기다렸다
        reply = await asyncio.wait_for(pending, timeout=2)
    assert json.loads(reply.data)["ok"] is True


def test_concurrency_from_env(monkeypatch):
    monkeypatch.delenv("LF_AI_CONCURRENCY", raising=False)
    assert Config.from_env().concurrency == 4  # 기본값
    monkeypatch.setenv("LF_AI_CONCURRENCY", "8")
    assert Config.from_env().concurrency == 8
    monkeypatch.setenv("LF_AI_CONCURRENCY", "0")
    with pytest.raises(ValueError):
        Config.from_env()
