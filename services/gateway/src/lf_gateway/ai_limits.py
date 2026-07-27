"""LLM API 비용·레이트 한도 관리 API — 설정 화면의 백엔드 (ADR-018 §3, ADR-020 §2).

집행은 ai-runtime의 BudgetGuard가 한다 (services/ai-runtime budget.py). gateway는
같은 Redis 키를 읽고 쓰는 창구일 뿐이다 — 한도 문서를 저장하면 다음 추론부터
(가드의 짧은 TTL 캐시만큼 뒤에) 반영되며, 서비스 재시작이 필요 없다.

계약 (FE 고정):
  GET /admin/ai-limits[?world_id=w_main]
      → {"limits": {...}, "usage": {...}, "available": bool}
        available=false는 Redis 미가용 — 화면은 값을 보이되 실측 없음을 알린다.
  PUT /admin/ai-limits[?world_id=w_main]
      → 검증(422) 후 저장, 저장본 + 갱신된 usage 반환. 저장 실패는 503 —
        "저장된 줄 알았는데 안 된" 상한은 없는 상한보다 위험하다.
게이트: admin.admin_guard (LF_ADMIN_TOKEN, dev는 열림).

⚠️ 아래 키·필드는 ai-runtime budget.py와 **합의된 고정 계약**이다 (프로젝터
이벤트 타입 계약과 같은 규약): 한쪽만 바꾸면 설정 화면이 집행되지 않는 값을
쓰게 된다. ai-runtime 패키지를 의존하지 않는 이유는 그쪽이 LLM SDK를 끌고
오기 때문이다 (ADR-018 — SDK는 ai-runtime에만).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from lf_gateway.admin import admin_guard
from lf_gateway.config import Config

logger = logging.getLogger("lf.gateway.ai_limits")

WORLD_PATTERN = r"^w_[a-z0-9_]+$"


def limits_key(env: str) -> str:
    return f"lf:{env}:ai:limits"


def spend_key(env: str, world_id: str, period: str) -> str:
    return f"lf:{env}:ai:spend:{world_id}:{period}"


def calls_key(env: str, world_id: str, day: str) -> str:
    return f"lf:{env}:ai:calls:{world_id}:{day}"


def tokens_key(env: str, world_id: str, day: str) -> str:
    return f"lf:{env}:ai:tokens:{world_id}:{day}"


def rpm_key(env: str, world_id: str, minute: int) -> str:
    return f"lf:{env}:ai:rpm:{world_id}:{minute}"


def unpriced_key(env: str) -> str:
    return f"lf:{env}:ai:unpriced"


class AiLimitsDoc(BaseModel):
    """한도 문서 — 0은 '끔'(상한 없음)이다.

    상한은 사람이 손으로 넣는 값이라 여기서 막는 게 마지막 방어선이다: 음수·
    0 나눗셈·터무니없는 값이 집행 경로로 가면 가드가 죽거나 무력해진다.

    기본값은 집행 쪽(budget.AiLimits)과 **같아야 한다** — 화면이 보여주는 값과
    실제로 걸리는 값이 갈리면 안 된다. 미설정 기본값은 dev 안전값이고, ADR-020 §2의
    Phase 1 예산($50/day)은 배포 env가 명시한다.
    """

    enabled: bool = True
    #: 분당 호출 상한 (0 = 끔)
    rpm: int = Field(60, ge=0, le=100_000)
    #: 일 지출 상한 USD (0 = 끔) — dev 안전값 (운영은 LF_AI_DAILY_USD로 명시)
    daily_usd: float = Field(5.0, ge=0, le=1_000_000)
    #: 월 지출 상한 USD (0 = 끔)
    monthly_usd: float = Field(0.0, ge=0, le=1_000_000)
    #: 이 비율을 넘으면 hot 티어를 warm으로 강등한다
    degrade_ratio: float = Field(0.8, ge=0.1, le=1.0)
    #: 응답 토큰 상한 (0 = 프로바이더 기본값)
    max_output_tokens: int = Field(0, ge=0, le=200_000)


def defaults_from_env() -> AiLimitsDoc:
    """저장본이 없을 때 보여줄 유효 한도 — ai-runtime의 env 바닥값과 같은 변수를 읽는다.

    (budget.limits_from_env의 거울. compose에서 두 서비스가 같은 .env를 읽는다는
    전제이며, 어긋나면 화면 값과 집행 값이 갈린다 — 상한은 저장 한 번으로 일치한다.)
    """
    fields: dict[str, Any] = {"enabled": os.environ.get("LF_AI_LIMITS_ENABLED", "1") != "0"}
    for name, key in (
        ("rpm", "LF_AI_RPM"),
        ("daily_usd", "LF_AI_DAILY_USD"),
        ("monthly_usd", "LF_AI_MONTHLY_USD"),
        ("degrade_ratio", "LF_AI_DEGRADE_RATIO"),
        ("max_output_tokens", "LF_AI_MAX_OUTPUT_TOKENS"),
    ):
        raw = os.environ.get(key)
        if raw:
            fields[name] = raw
    try:
        return AiLimitsDoc(**fields)
    except Exception as e:  # env 오타가 설정 화면을 죽이면 안 된다
        logger.warning("LF_AI_* 기본 한도를 읽지 못해 코드 기본값을 쓴다: %s", e)
        return AiLimitsDoc()


async def _read_float(redis: Any, key: str) -> float:
    raw = await redis.get(key)
    return float(raw) if raw else 0.0


async def _read_int(redis: Any, key: str) -> int:
    raw = await redis.get(key)
    return int(raw) if raw else 0


async def read_limits(redis: Any, env: str) -> AiLimitsDoc:
    """저장본 → 문서. 없거나 손상됐으면 env 바닥값 (집행 쪽과 같은 방어)."""
    raw = await redis.get(limits_key(env))
    if not raw:
        return defaults_from_env()
    try:
        return AiLimitsDoc(**{**defaults_from_env().model_dump(), **json.loads(raw)})
    except Exception as e:
        logger.warning("저장된 한도 문서가 손상됐다 — 기본값으로 보인다: %s", e)
        return defaults_from_env()


async def read_usage(redis: Any, env: str, world_id: str) -> dict[str, Any]:
    """현재 사용량 — 카운터는 UTC 경계로 끊긴다 (집행 쪽과 같은 규약)."""
    now = datetime.now(UTC)
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    minute = int(now.timestamp()) // 60
    unpriced = await redis.smembers(unpriced_key(env))
    return {
        "day": day,
        "month": month,
        "day_usd": round(await _read_float(redis, spend_key(env, world_id, day)), 6),
        "month_usd": round(await _read_float(redis, spend_key(env, world_id, month)), 6),
        "calls_today": await _read_int(redis, calls_key(env, world_id, day)),
        "tokens_today": await _read_int(redis, tokens_key(env, world_id, day)),
        "rpm_current": await _read_int(redis, rpm_key(env, world_id, minute)),
        # 단가 미등재 모델 — 보수적 단가로 셈되고 있다는 경고 재료 (pricing.py)
        "unpriced_models": sorted(
            m.decode() if isinstance(m, bytes) else str(m) for m in (unpriced or ())
        ),
    }


def create_ai_limits_router(cfg: Config) -> APIRouter:
    router = APIRouter(prefix="/admin", dependencies=[Depends(admin_guard(cfg))])

    @router.get("/ai-limits")
    async def get_ai_limits(
        request: Request, world_id: str = Query("w_main", pattern=WORLD_PATTERN)
    ) -> dict[str, Any]:
        # 조회는 조용히 강등한다 — Redis가 없어도 설정 화면은 서고, 값을 고칠 수
        # 있어야 한다 (라이브 피드·그래프와 같은 규약)
        try:
            redis = request.app.state.redis
            limits = await read_limits(redis, cfg.env)
            usage = await read_usage(redis, cfg.env, world_id)
            available = True
        except Exception as e:
            logger.warning("한도·사용량 조회 실패(기본값으로 응답): %s", e)
            limits, usage, available = defaults_from_env(), None, False
        return {
            "world_id": world_id,
            "limits": limits.model_dump(),
            "usage": usage,
            "available": available,
        }

    @router.put("/ai-limits")
    async def put_ai_limits(
        doc: AiLimitsDoc,
        request: Request,
        world_id: str = Query("w_main", pattern=WORLD_PATTERN),
    ) -> dict[str, Any]:
        # 저장은 강등하지 않는다: 실패를 성공으로 보이면 사용자는 상한이 걸린 줄
        # 알고 세계를 돌린다 — 비용 사고의 정확한 모양이다
        try:
            redis = request.app.state.redis
            await redis.set(
                limits_key(cfg.env), json.dumps(doc.model_dump(), ensure_ascii=False)
            )
        except Exception as e:
            logger.warning("한도 저장 실패: %s", e)
            raise HTTPException(503, f"한도를 저장하지 못했다 (Redis 확인): {e}") from e
        usage = None
        try:
            usage = await read_usage(redis, cfg.env, world_id)
        except Exception as e:  # 저장은 됐다 — 사용량 조회 실패는 화면만 비운다
            logger.warning("저장 후 사용량 조회 실패: %s", e)
        logger.info(
            "LLM 한도 저장 (env=%s): 켬=%s 일 $%.2f 월 $%.2f 분당 %d회 강등 %.0f%% 출력상한 %d",
            cfg.env, doc.enabled, doc.daily_usd, doc.monthly_usd, doc.rpm,
            doc.degrade_ratio * 100, doc.max_output_tokens,
        )
        return {
            "world_id": world_id,
            "limits": doc.model_dump(),
            "usage": usage,
            "available": usage is not None,
        }

    return router
