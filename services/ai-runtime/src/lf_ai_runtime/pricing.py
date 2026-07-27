"""모델 단가 표 — 비용 상한 집행의 환율 (ADR-018 §3, ADR-020 §2 $50/day 하드 캡).

단가는 벤더 가격표를 따르므로 코드가 아니라 **설정**이다: 아래 표는 씨앗이고
`LF_MODEL_PRICES`로 재정의한다 (프로바이더·모델 매핑과 같은 규약, ADR-018 §배치).

    LF_MODEL_PRICES='{"gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125}}'

단위는 USD / 1M tokens. 표의 값은 2026-07-27 벤더 가격표 기준이다.

**모르는 모델은 FALLBACK_PRICE(보수적 = 최상위 티어)로 계산하고 이름을 남긴다.**
비용 가드가 '모르는 모델은 공짜'로 새는 쪽이 훨씬 위험하다 — 상한이 조용히
무력화되면 청구서로만 알게 된다. 대신 실제 단가를 알면 LF_MODEL_PRICES로
바로잡을 수 있고, 미등재 모델 목록은 설정 화면에 경고로 올라간다.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from lf_ai_runtime.model import Usage

logger = logging.getLogger("lf.ai_runtime.pricing")


@dataclass(frozen=True)
class Price:
    """USD / 1M tokens. cache_read·cache_write는 프롬프트 캐시 단가 (ADR-009 프리픽스 캐시)."""

    input: float
    output: float
    cache_read: float = 0.0
    #: 캐시 기록 프리미엄. Anthropic은 입력의 1.25배(5분 TTL), OpenAI·Gemini는
    #: 자동 캐시라 기록 프리미엄이 없어 입력 단가와 같게 둔다.
    cache_write: float = 0.0


#: 자체 호스팅·규칙 프로바이더 — 토큰당 청구가 존재하지 않는다 (GPU 고정비는 예산 밖)
FREE_PROVIDERS = frozenset({"rule", "local"})

#: 미등재 모델의 보수적 단가 — 최상위 티어로 셈해 상한이 늦게 걸리지 않게 한다
FALLBACK_PRICE = Price(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)

#: 모델 단가 씨앗 표 (2026-07-27 벤더 가격표). 키는 모델명 프리픽스로도 쓰인다 —
#: 날짜 스냅샷(claude-haiku-4-5-20251001)은 최장 프리픽스 일치로 흡수된다.
MODEL_PRICES: dict[str, Price] = {
    # Anthropic — 캐시 읽기 0.1배, 기록 1.25배(5분 TTL)
    "claude-fable-5": Price(10.0, 50.0, 1.0, 12.5),
    "claude-opus-5": Price(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": Price(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-7": Price(5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-6": Price(5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": Price(3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-6": Price(3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-5": Price(1.0, 5.0, 0.1, 1.25),
    # Gemini — 200k 이하 프롬프트 기준 (초과 구간은 pro가 2배, LF_MODEL_ROUTES 규모 밖)
    "gemini-2.5-pro": Price(1.25, 10.0, 0.125, 1.25),
    "gemini-2.5-flash": Price(0.30, 2.50, 0.03, 0.30),
    # OpenAI — 자동 캐시(기록 프리미엄 없음). ⚠️ 현행 가격표에 gpt-5/gpt-5-mini는
    # 없다(모델 세대가 지났다): runtime.DEFAULT_TIER_MODELS의 openai 기본 라우트를
    # 쓰면 미등재로 잡혀 FALLBACK_PRICE로 셈해진다 — 실제 쓰는 모델을
    # LF_MODEL_ROUTES로 지정하거나 단가를 LF_MODEL_PRICES로 넣어라.
    "gpt-5.6-sol": Price(5.0, 30.0, 0.5, 5.0),
    "gpt-5.6-terra": Price(2.5, 15.0, 0.25, 2.5),
    "gpt-5.6-luna": Price(1.0, 6.0, 0.1, 1.0),
    "gpt-5.5": Price(5.0, 30.0, 0.5, 5.0),
    "gpt-5.4": Price(2.5, 15.0, 0.25, 2.5),
    "gpt-5.4-mini": Price(0.75, 4.5, 0.075, 0.75),
}

_PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")


def load_price_overrides(raw: str | None = None) -> dict[str, Price]:
    """LF_MODEL_PRICES 재정의를 읽는다 — 빠진 필드는 표의 값(없으면 0)을 잇는다."""
    raw = os.environ.get("LF_MODEL_PRICES") if raw is None else raw
    if not raw:
        return {}
    overrides: dict[str, Price] = {}
    for model, spec in json.loads(raw).items():
        base = MODEL_PRICES.get(model)
        values = {
            field: float(spec.get(field, getattr(base, field, 0.0) if base else 0.0))
            for field in _PRICE_FIELDS
        }
        overrides[model] = Price(**values)
    return overrides


class PriceBook:
    """모델 → 단가. 미등재 모델은 보수적 단가로 셈하고 이름을 모아둔다."""

    def __init__(self, overrides: dict[str, Price] | None = None) -> None:
        self._prices = {**MODEL_PRICES, **(overrides or {})}
        # 최장 프리픽스 우선 — "claude-opus-4-8"이 "claude-opus-4"보다 먼저 걸린다
        self._prefixes = sorted(self._prices, key=len, reverse=True)
        self._unpriced: set[str] = set()

    @property
    def unpriced(self) -> frozenset[str]:
        """단가를 몰라 보수적으로 셈한 모델들 — 설정 화면의 경고 재료."""
        return frozenset(self._unpriced)

    def price(self, provider: str, model: str) -> Price:
        if provider in FREE_PROVIDERS:
            return Price(0.0, 0.0)
        exact = self._prices.get(model)
        if exact is not None:
            return exact
        for prefix in self._prefixes:
            if model.startswith(prefix):
                return self._prices[prefix]
        if model not in self._unpriced:  # 로그 도배 방지 — 모델당 한 번
            self._unpriced.add(model)
            logger.warning(
                "단가 미등재 모델 '%s' — 보수적 단가(입력 $%.2f/출력 $%.2f per 1M)로 "
                "비용을 셈한다. 실제 단가는 LF_MODEL_PRICES로 넣어라",
                model, FALLBACK_PRICE.input, FALLBACK_PRICE.output,
            )
        return FALLBACK_PRICE

    def cost_usd(self, provider: str, model: str, usage: Usage) -> float:
        """이번 호출의 USD 비용.

        usage.input_tokens는 **캐시 밖 입력만**이다 (프로바이더 어댑터가 캐시분을
        떼어 담는다) — 캐시 토큰과 겹세지 않는다.
        """
        price = self.price(provider, model)
        return (
            usage.input_tokens * price.input
            + usage.output_tokens * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * price.cache_write
        ) / 1_000_000
