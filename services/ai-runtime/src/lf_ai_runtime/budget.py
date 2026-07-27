"""예산·레이트 집행 — 청구 폭발을 구조적으로 불가능하게 한다 (ADR-018 §3, ADR-020 §2).

정책은 두 ADR이 정한 것을 그대로 집행한다:
- 누적 지출이 상한의 `degrade_ratio`(기본 80%)를 넘으면 **티어 강등** — hot 요청을
  warm 모델로 내린다 (ADR-018 §3 "소진 시 등급 강등", ADR-020 §4 "80% 도달 시 경고").
- 상한을 넘으면 **명시적 오류로 거절**한다. 액터는 규칙 행동으로 폴백하고
  (`params.fallback: true`, ADR-012) 세계는 저품질로나마 계속 돈다 — 조용한
  성공 위장 없이, 화면에서 구분되는 강등이다 (ADR-018 §4).
- 분당 호출 수(RPM) 상한도 같은 경로로 거절한다 — 벤더 레이트리밋에 부딪혀
  재시도 폭풍이 되기 전에 우리 쪽에서 먼저 막는다.

카운터는 **Redis에 산다**: ai-runtime은 무상태 다중 인스턴스라(ADR-019) 프로세스
안의 카운터로는 세계 단위 상한을 집행할 수 없다. Redis가 없으면 프로세스 안
카운터(MemoryStore)로 강등되며, 그때는 상한이 인스턴스별로만 걸린다 — 경고를 남긴다.

한도 문서(`AiLimits`)도 같은 Redis 키에 산다. 설정 화면(gateway /admin/ai-limits)이
쓰고 여기서 짧은 TTL로 읽으므로, 재시작 없이 수 초 안에 반영된다.

키 계약 (gateway ai_limits.py와 합의된 고정 계약 — 한쪽만 바꾸면 안 된다):
    lf:{env}:ai:limits                        한도 문서 JSON
    lf:{env}:ai:spend:{world}:{YYYY-MM-DD}    일 누적 USD (UTC 경계)
    lf:{env}:ai:spend:{world}:{YYYY-MM}       월 누적 USD (UTC 경계)
    lf:{env}:ai:calls:{world}:{YYYY-MM-DD}    일 호출 수
    lf:{env}:ai:tokens:{world}:{YYYY-MM-DD}   일 토큰 수
    lf:{env}:ai:rpm:{world}:{epoch_minute}    분 창 호출 수
    lf:{env}:ai:unpriced                      단가 미등재로 보수적 계산된 모델 이름
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from lf_ai_runtime.model import Usage
from lf_ai_runtime.pricing import FALLBACK_PRICE, PriceBook

logger = logging.getLogger("lf.ai_runtime.budget")

#: 일 지출 키 보관 기간 — 월 집계와 지난 달 대조에 넉넉히
DAY_TTL_S = 40 * 24 * 3600
MONTH_TTL_S = 400 * 24 * 3600
#: 분 창 키 — 창 하나만 지나면 쓸모없다
RPM_TTL_S = 120
UNPRICED_TTL_S = 30 * 24 * 3600

#: 강등 사다리 — hot 요청을 한 칸 내린다 (system은 이미 소형이라 내릴 곳이 없다)
DEGRADE_TO = {"hot": "warm"}


@dataclass(frozen=True)
class AiLimits:
    """LLM API 비용·레이트 한도. 0은 '끔'이다 (상한 없음).

    **아무것도 설정하지 않았을 때의 기본값은 dev 안전값이다** — ADR-020 §2의 Phase 1
    예산($50/day/세계)이 아니다. 비용 가드의 미설정 기본값은 싼 쪽으로 실패해야
    한다: 개인 키로 로컬을 돌리다 루프가 밤새 도는 쪽이 훨씬 흔한 사고이고,
    상한이 낮아 강등된 세계는 되돌릴 수 있지만 청구서는 되돌릴 수 없다.
    Phase 1 예산은 배포 설정이 명시한다 (LF_AI_DAILY_USD=50, ADR-018 §배치의
    "프로바이더·모델 매핑은 전부 설정" 규약과 같은 결).
    """

    enabled: bool = True
    #: 분당 호출 상한 (0 = 끔). ADR-020 추정 부하는 ~700/h ≈ 12/min
    rpm: int = 60
    #: 일 지출 상한 USD (0 = 끔) — dev 안전값. 운영은 env로 ADR-020 값을 명시한다
    daily_usd: float = 5.0
    #: 월 지출 상한 USD (0 = 끔)
    monthly_usd: float = 0.0
    #: 이 비율을 넘으면 hot을 warm으로 강등한다 (0.8 = 80%)
    degrade_ratio: float = 0.8
    #: 응답 토큰 상한 (0 = 프로바이더 기본값 사용). 출력 단가가 입력의 5배라
    #: 가장 직접적인 비용 손잡이지만, 추론 모델(gpt-5 계열)은 추론 토큰이 이
    #: 예산을 함께 쓰므로 너무 낮게 잡으면 응답이 잘린다 — 기본은 끔.
    max_output_tokens: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "rpm": self.rpm,
            "daily_usd": self.daily_usd,
            "monthly_usd": self.monthly_usd,
            "degrade_ratio": self.degrade_ratio,
            "max_output_tokens": self.max_output_tokens,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], *, base: AiLimits | None = None) -> AiLimits:
        """저장본을 읽는다 — 빠진 키는 base(없으면 기본값), 이상한 값은 되잡는다.

        읽는 쪽이 방어한다: 한도 문서는 외부(설정 화면·수기 편집)에서 오므로
        음수 상한이나 0 나눗셈이 집행 경로로 새어들면 가드 자체가 죽는다.
        """
        base = base or cls()
        raw_ratio = _as_float(data.get("degrade_ratio"), base.degrade_ratio)
        return cls(
            enabled=bool(data.get("enabled", base.enabled)),
            rpm=max(0, _as_int(data.get("rpm"), base.rpm)),
            daily_usd=max(0.0, _as_float(data.get("daily_usd"), base.daily_usd)),
            monthly_usd=max(0.0, _as_float(data.get("monthly_usd"), base.monthly_usd)),
            # 0 이하·1 초과는 강등을 무의미하게 만든다 (즉시 강등 / 강등 없음)
            degrade_ratio=min(1.0, max(0.1, raw_ratio)),
            max_output_tokens=max(
                0, _as_int(data.get("max_output_tokens"), base.max_output_tokens)
            ),
        )


def _as_int(value: Any, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def _as_float(value: Any, fallback: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class Decision:
    """check()의 판정 — 통과 여부, 실제로 쓸 티어, 거절 사유."""

    allow: bool
    tier: str
    reason: str | None = None
    #: 예산 80% 초과로 티어가 내려갔다 (품질 강등의 관측 지점)
    degraded: bool = False
    #: 응답 토큰 상한 (None = 프로바이더 기본값)
    max_output_tokens: int | None = None


class MemoryStore:
    """Redis 미가용 시의 프로세스 안 대역 — redis.asyncio의 쓰는 부분만 흉내낸다.

    ⚠️ 인스턴스별 카운터다: 다중 인스턴스에서 세계 단위 상한을 집행하지 못한다.
    dev·테스트 용도이며, 운영은 Redis를 붙여야 상한이 진짜 상한이 된다.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> bytes | None:
        raw = self._values.get(key)
        return None if raw is None else raw.encode()

    async def set(self, key: str, value: str) -> None:
        self._values[key] = value

    async def incr(self, key: str) -> int:
        value = int(self._values.get(key, "0")) + 1
        self._values[key] = str(value)
        return value

    async def incrby(self, key: str, amount: int) -> int:
        value = int(self._values.get(key, "0")) + amount
        self._values[key] = str(value)
        return value

    async def incrbyfloat(self, key: str, amount: float) -> float:
        value = float(self._values.get(key, "0")) + amount
        self._values[key] = repr(value)
        return value

    async def expire(self, key: str, seconds: int) -> None:
        return None  # 프로세스 수명이 곧 TTL이다

    async def sadd(self, key: str, *members: str) -> int:
        bucket = self._sets.setdefault(key, set())
        before = len(bucket)
        bucket.update(members)
        return len(bucket) - before

    async def smembers(self, key: str) -> set[bytes]:
        return {m.encode() for m in self._sets.get(key, set())}


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


def periods(now: datetime) -> tuple[str, str]:
    """(일, 월) 키 조각 — UTC 경계다 (인스턴스 시간대에 상한이 흔들리지 않게)."""
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


class BudgetGuard:
    """호출 전 판정(check) + 호출 후 계량(record). 실패가 세계를 멈추지 않는다.

    저장소 오류는 삼킨다: 카운터를 못 읽어 추론을 막으면 Redis 장애가 곧 세계
    정지가 된다. 대신 경고를 남기고 통과시킨다 — 가용성이 상한보다 앞선다.
    (상한이 반드시 걸려야 하는 배포는 Redis 가용성으로 보장하라.)
    """

    def __init__(
        self,
        env: str,
        store: Any,
        *,
        defaults: AiLimits | None = None,
        prices: PriceBook | None = None,
        limits_ttl_s: float = 3.0,
        now_fn: Any = None,
        monotonic_fn: Any = None,
    ) -> None:
        self._env = env
        self._store = store
        self._defaults = defaults or AiLimits()
        self._prices = prices or PriceBook()
        self._limits_ttl_s = limits_ttl_s
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_fn or time.monotonic
        self._cached: AiLimits = self._defaults
        self._cached_at: float = -1.0

    @property
    def prices(self) -> PriceBook:
        return self._prices

    async def limits(self) -> AiLimits:
        """현재 한도 — 짧은 TTL 캐시. 설정 변경이 재시작 없이 수 초 안에 닿는다."""
        now = self._monotonic()
        if self._cached_at >= 0 and now - self._cached_at < self._limits_ttl_s:
            return self._cached
        limits = self._defaults
        try:
            raw = await self._store.get(limits_key(self._env))
            if raw:
                limits = AiLimits.from_json(json.loads(raw), base=self._defaults)
        except Exception as e:
            logger.warning("한도 문서를 읽지 못해 env 기본값으로 진행한다: %s", e)
        self._cached, self._cached_at = limits, now
        return limits

    async def check(self, world_id: str, tier: str) -> Decision:
        limits = await self.limits()
        if not limits.enabled:
            return Decision(True, tier)
        cap = _output_cap(limits)
        try:
            return await self._check(limits, world_id, tier, cap)
        except Exception as e:
            # 카운터 미가용 — 막지 않는다 (가용성 우선). 상한이 이 순간 무력하다는
            # 사실은 경고로 남긴다
            logger.warning("예산 카운터를 읽지 못해 통과시킨다(상한 일시 무력): %s", e)
            return Decision(True, tier, max_output_tokens=cap)

    async def _check(
        self, limits: AiLimits, world_id: str, tier: str, cap: int | None
    ) -> Decision:
        now = self._now()
        day, month = periods(now)
        # 지출을 먼저 본다 — 이미 소진이면 분 창 슬롯을 쓰지 않고 거절한다
        day_spend = await self._read_float(spend_key(self._env, world_id, day))
        month_spend = await self._read_float(spend_key(self._env, world_id, month))
        exhausted = _exhausted(limits, day_spend, month_spend)
        if exhausted is not None:
            return Decision(False, tier, reason=exhausted, max_output_tokens=cap)

        if limits.rpm > 0:
            minute = int(now.timestamp()) // 60
            key = rpm_key(self._env, world_id, minute)
            # INCR 후 비교 — 다중 인스턴스에서 read-then-incr 경합 없이 정확하다.
            # 거절된 호출도 창 카운트를 올리지만 창은 1분이면 새로 열린다.
            count = await self._store.incr(key)
            await self._store.expire(key, RPM_TTL_S)
            if count > limits.rpm:
                return Decision(
                    False, tier,
                    reason=(
                        f"레이트 리밋 초과: 분당 {limits.rpm}회 상한 (이번 창 {count}회) — "
                        "잠시 뒤 다시 시도하라"
                    ),
                    max_output_tokens=cap,
                )

        degraded_tier = _degraded(limits, tier, day_spend, month_spend)
        if degraded_tier is not None:
            logger.info(
                "예산 %.0f%% 초과 — 티어 강등 %s→%s (world=%s, 일 지출 $%.4f)",
                limits.degrade_ratio * 100, tier, degraded_tier, world_id, day_spend,
            )
            return Decision(True, degraded_tier, degraded=True, max_output_tokens=cap)
        return Decision(True, tier, max_output_tokens=cap)

    async def record(
        self, world_id: str, provider: str, model: str, usage: Usage
    ) -> float:
        """이번 호출의 비용을 누적하고 USD를 반환한다 (관측용).

        스키마 검증 실패·수정 재시도분도 호출자가 그대로 기록한다 — 토큰은 이미
        나갔다. 쓰지 않은 것처럼 셈하면 상한이 실제 청구보다 늦게 걸린다.
        """
        cost = self._prices.cost_usd(provider, model, usage)
        day, month = periods(self._now())
        try:
            await self._add_float(spend_key(self._env, world_id, day), cost, DAY_TTL_S)
            await self._add_float(spend_key(self._env, world_id, month), cost, MONTH_TTL_S)
            calls = calls_key(self._env, world_id, day)
            await self._store.incr(calls)
            await self._store.expire(calls, DAY_TTL_S)
            if usage.total_tokens:
                tokens = tokens_key(self._env, world_id, day)
                await self._store.incrby(tokens, usage.total_tokens)
                await self._store.expire(tokens, DAY_TTL_S)
            if model in self._prices.unpriced:
                key = unpriced_key(self._env)
                await self._store.sadd(key, model)
                await self._store.expire(key, UNPRICED_TTL_S)
        except Exception as e:
            # 계량 실패가 추론 결과를 버리게 하면 안 된다 — 이 호출은 이미 성공했다
            logger.warning("사용량 기록 실패(무시): %s", e)
        return cost

    async def snapshot(self, world_id: str) -> dict[str, Any]:
        """설정 화면이 보여줄 현재 사용량 — 실패해도 화면은 서야 한다."""
        limits = await self.limits()
        day, month = periods(self._now())
        minute = int(self._now().timestamp()) // 60
        available = True
        day_usd = month_usd = 0.0
        calls = tokens = rpm_now = 0
        unpriced: list[str] = sorted(self._prices.unpriced)
        try:
            day_usd = await self._read_float(spend_key(self._env, world_id, day))
            month_usd = await self._read_float(spend_key(self._env, world_id, month))
            calls = await self._read_int(calls_key(self._env, world_id, day))
            tokens = await self._read_int(tokens_key(self._env, world_id, day))
            rpm_now = await self._read_int(rpm_key(self._env, world_id, minute))
            stored = await self._store.smembers(unpriced_key(self._env))
            names = {m.decode() if isinstance(m, bytes) else str(m) for m in stored}
            unpriced = sorted(names | set(unpriced))
        except Exception as e:
            logger.warning("사용량 조회 실패: %s", e)
            available = False
        return {
            "limits": limits.to_json(),
            "usage": {
                "day": day,
                "month": month,
                "day_usd": round(day_usd, 6),
                "month_usd": round(month_usd, 6),
                "calls_today": calls,
                "tokens_today": tokens,
                "rpm_current": rpm_now,
                "unpriced_models": unpriced,
                "fallback_price_usd_per_mtok": {
                    "input": FALLBACK_PRICE.input,
                    "output": FALLBACK_PRICE.output,
                },
            },
            "available": available,
        }

    async def _read_float(self, key: str) -> float:
        raw = await self._store.get(key)
        return float(raw) if raw else 0.0

    async def _read_int(self, key: str) -> int:
        raw = await self._store.get(key)
        return int(raw) if raw else 0

    async def _add_float(self, key: str, amount: float, ttl_s: int) -> None:
        if amount <= 0:
            return  # 규칙·로컬 프로바이더는 비용 0 — 키를 만들지 않는다
        await self._store.incrbyfloat(key, amount)
        await self._store.expire(key, ttl_s)


def _output_cap(limits: AiLimits) -> int | None:
    return limits.max_output_tokens or None


def _exhausted(limits: AiLimits, day_spend: float, month_spend: float) -> str | None:
    if limits.daily_usd > 0 and day_spend >= limits.daily_usd:
        return (
            f"일 비용 상한 소진: ${day_spend:.2f} / ${limits.daily_usd:.2f} — "
            "상한을 올리거나 다음 날까지 기다려라 (설정 › LLM API)"
        )
    if limits.monthly_usd > 0 and month_spend >= limits.monthly_usd:
        return (
            f"월 비용 상한 소진: ${month_spend:.2f} / ${limits.monthly_usd:.2f} — "
            "상한을 올리거나 다음 달까지 기다려라 (설정 › LLM API)"
        )
    return None


def _degraded(
    limits: AiLimits, tier: str, day_spend: float, month_spend: float
) -> str | None:
    """강등 대상 티어 — 상한의 degrade_ratio를 넘겼고 내릴 칸이 있을 때만."""
    target = DEGRADE_TO.get(tier)
    if target is None:
        return None
    over_day = limits.daily_usd > 0 and day_spend >= limits.daily_usd * limits.degrade_ratio
    over_month = (
        limits.monthly_usd > 0 and month_spend >= limits.monthly_usd * limits.degrade_ratio
    )
    return target if over_day or over_month else None


def limits_from_env(env_map: Any = None) -> AiLimits:
    """환경변수 기본 한도 — Redis에 저장본이 없을 때의 바닥값."""
    import os

    source = os.environ if env_map is None else env_map
    base = AiLimits()
    return AiLimits.from_json(
        {
            "enabled": source.get("LF_AI_LIMITS_ENABLED", "1") != "0",
            "rpm": source.get("LF_AI_RPM", base.rpm),
            "daily_usd": source.get("LF_AI_DAILY_USD", base.daily_usd),
            "monthly_usd": source.get("LF_AI_MONTHLY_USD", base.monthly_usd),
            "degrade_ratio": source.get("LF_AI_DEGRADE_RATIO", base.degrade_ratio),
            "max_output_tokens": source.get(
                "LF_AI_MAX_OUTPUT_TOKENS", base.max_output_tokens
            ),
        },
        base=base,
    )


def with_defaults(limits: AiLimits, **changes: Any) -> AiLimits:
    """테스트·설정 조립용 얕은 갱신."""
    return replace(limits, **changes)
