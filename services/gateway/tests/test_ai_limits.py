"""LLM 한도 관리 API 검증 (ai_limits.py — ADR-018 §3, ADR-020 §2).

인프라 없이 돈다: 최소 redis 대역을 주입한다 (test_push의 _StoreRedis 선례).
겨누는 것은 ① 왕복 편집(GET→PUT→GET)이 집행 쪽이 읽는 그 키에 쓰인다
② 이상한 상한은 422로 막힌다 ③ 조회는 조용히 강등하고 **저장은 강등하지 않는다**
④ 관리 토큰 게이트가 이 경로에도 걸린다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from lf_gateway.ai_limits import (
    calls_key,
    limits_key,
    rpm_key,
    spend_key,
    tokens_key,
    unpriced_key,
)
from lf_gateway.config import Config
from lf_gateway.main import create_app

ENV = "test"
WORLD = "w_main"


class FakeRedis:
    """한도·카운터 경로만 받는 최소 redis 대역 (문자열 값 + 집합)."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> bytes | None:
        raw = self.values.get(key)
        return None if raw is None else raw.encode()

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def smembers(self, key: str) -> set[bytes]:
        return {m.encode() for m in self.sets.get(key, set())}


class BrokenRedis(FakeRedis):
    async def get(self, key: str):
        raise RuntimeError("redis down")

    async def set(self, key: str, value: str):
        raise RuntimeError("redis down")


def make_client(redis, *, admin_token: str | None = None) -> httpx.AsyncClient:
    cfg = Config(nats_url="unused(주입)", env=ENV, admin_token=admin_token)
    app = create_app(cfg, redis=redis)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def doc(**over) -> dict:
    base = {
        "enabled": True,
        "rpm": 30,
        "daily_usd": 5.0,
        "monthly_usd": 100.0,
        "degrade_ratio": 0.75,
        "max_output_tokens": 512,
    }
    return {**base, **over}


def test_redis_key_contract_is_literal():
    """ai-runtime budget.py와 **글자 단위로** 같아야 한다 (그쪽에도 같은 단정이 있다).

    두 패키지는 서로를 의존하지 않는다 (ADR-018 — LLM SDK는 ai-runtime에만).
    계약은 이 리터럴 단정이 지킨다: 키 형식을 고치면 여기서 먼저 깨진다.
    """
    assert limits_key("dev") == "lf:dev:ai:limits"
    assert spend_key("dev", "w_main", "2026-07-27") == "lf:dev:ai:spend:w_main:2026-07-27"
    assert spend_key("dev", "w_main", "2026-07") == "lf:dev:ai:spend:w_main:2026-07"
    assert calls_key("dev", "w_main", "2026-07-27") == "lf:dev:ai:calls:w_main:2026-07-27"
    assert tokens_key("dev", "w_main", "2026-07-27") == "lf:dev:ai:tokens:w_main:2026-07-27"
    assert rpm_key("dev", "w_main", 29558400) == "lf:dev:ai:rpm:w_main:29558400"
    assert unpriced_key("dev") == "lf:dev:ai:unpriced"


async def test_get_returns_env_defaults_before_first_save():
    redis = FakeRedis()
    async with make_client(redis) as client:
        body = (await client.get("/admin/ai-limits")).json()
    assert body["available"] is True
    assert body["limits"]["enabled"] is True
    # 미설정 기본값은 dev 안전값이고 집행 쪽(budget.AiLimits)과 같아야 한다 —
    # ADR-020 §2의 Phase 1 예산($50/day)은 배포 env가 명시한다
    assert body["limits"]["daily_usd"] == 5.0
    assert body["usage"]["day_usd"] == 0.0


async def test_put_saves_to_the_key_the_guard_reads():
    """저장 위치가 집행 위치다 — 다른 키에 쓰면 화면만 바뀌고 상한은 그대로다."""
    redis = FakeRedis()
    async with make_client(redis) as client:
        saved = (await client.put("/admin/ai-limits", json=doc())).json()
        reread = (await client.get("/admin/ai-limits")).json()
    assert saved["limits"]["rpm"] == 30
    assert reread["limits"] == saved["limits"]  # 왕복 편집이 남는다
    stored = json.loads(redis.values[limits_key(ENV)])
    assert stored["daily_usd"] == 5.0 and stored["max_output_tokens"] == 512


async def test_put_accepts_zero_as_no_cap():
    async with make_client(FakeRedis()) as client:
        body = (await client.put("/admin/ai-limits", json=doc(rpm=0, daily_usd=0))).json()
    assert body["limits"]["rpm"] == 0 and body["limits"]["daily_usd"] == 0.0


@pytest.mark.parametrize(
    "over",
    [
        {"rpm": -1},               # 음수 상한은 상한이 아니다
        {"daily_usd": -0.5},
        {"degrade_ratio": 0},      # 0은 즉시 강등 — 무의미하다
        {"degrade_ratio": 1.5},    # 1 초과는 강등이 없다는 뜻
        {"max_output_tokens": -1},
        {"rpm": "많이"},
    ],
)
async def test_put_rejects_hostile_values(over):
    redis = FakeRedis()
    async with make_client(redis) as client:
        response = await client.put("/admin/ai-limits", json=doc(**over))
    assert response.status_code == 422
    assert limits_key(ENV) not in redis.values  # 거부된 한도는 저장되지 않는다


async def test_usage_snapshot_reads_the_shared_counters():
    redis = FakeRedis()
    now = datetime.now(UTC)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    redis.values[spend_key(ENV, WORLD, day)] = "1.25"
    redis.values[spend_key(ENV, WORLD, month)] = "9.5"
    redis.values[calls_key(ENV, WORLD, day)] = "42"
    redis.values[tokens_key(ENV, WORLD, day)] = "123456"
    redis.values[rpm_key(ENV, WORLD, int(now.timestamp()) // 60)] = "7"
    redis.sets[unpriced_key(ENV)] = {"gpt-5", "glm-4.6"}
    async with make_client(redis) as client:
        usage = (await client.get("/admin/ai-limits")).json()["usage"]
    assert usage["day_usd"] == 1.25 and usage["month_usd"] == 9.5
    assert usage["calls_today"] == 42 and usage["tokens_today"] == 123456
    assert usage["rpm_current"] == 7
    assert usage["unpriced_models"] == ["glm-4.6", "gpt-5"]  # 단가 경고 재료


async def test_usage_is_scoped_per_world():
    redis = FakeRedis()
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    redis.values[spend_key(ENV, "w_side", day)] = "3.0"
    async with make_client(redis) as client:
        main = (await client.get("/admin/ai-limits")).json()
        side = (await client.get("/admin/ai-limits?world_id=w_side")).json()
    assert main["usage"]["day_usd"] == 0.0
    assert side["usage"]["day_usd"] == 3.0 and side["world_id"] == "w_side"


async def test_get_degrades_quietly_when_redis_is_down():
    """설정 화면은 서야 한다 — 값을 못 읽어도 고칠 수는 있어야 한다."""
    async with make_client(BrokenRedis()) as client:
        body = (await client.get("/admin/ai-limits")).json()
    assert body["available"] is False and body["usage"] is None
    assert body["limits"]["daily_usd"] == 5.0


async def test_put_does_not_degrade_quietly_when_redis_is_down():
    """저장 실패를 성공으로 보이면 사용자는 상한이 걸린 줄 알고 세계를 돌린다."""
    async with make_client(BrokenRedis()) as client:
        response = await client.put("/admin/ai-limits", json=doc())
    assert response.status_code == 503
    assert "저장하지 못했다" in response.json()["detail"]


async def test_corrupt_stored_document_shows_defaults():
    redis = FakeRedis()
    redis.values[limits_key(ENV)] = "{ not json"
    async with make_client(redis) as client:
        body = (await client.get("/admin/ai-limits")).json()
    assert body["limits"]["daily_usd"] == 5.0  # 죽지 않고 기본값으로 보인다


async def test_admin_token_gates_this_route_too():
    """게이트가 라우터마다 갈리면 한쪽만 열린 채 남는다 — 같은 게이트를 쓴다."""
    redis = FakeRedis()
    async with make_client(redis, admin_token="s3cret") as client:
        assert (await client.get("/admin/ai-limits")).status_code == 403
        assert (await client.put("/admin/ai-limits", json=doc())).status_code == 403
        headers = {"Authorization": "Bearer s3cret"}
        assert (await client.get("/admin/ai-limits", headers=headers)).status_code == 200
        assert (
            await client.put("/admin/ai-limits", json=doc(), headers=headers)
        ).status_code == 200


async def test_env_defaults_are_read_from_the_same_vars_as_the_guard(monkeypatch):
    monkeypatch.setenv("LF_AI_DAILY_USD", "2.5")
    monkeypatch.setenv("LF_AI_RPM", "12")
    monkeypatch.setenv("LF_AI_LIMITS_ENABLED", "0")
    async with make_client(FakeRedis()) as client:
        limits = (await client.get("/admin/ai-limits")).json()["limits"]
    assert limits["daily_usd"] == 2.5 and limits["rpm"] == 12
    assert limits["enabled"] is False
