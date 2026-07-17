"""SNS 포스팅 리듬 — "이 tick이 이 액터의 근황을 남기고 싶은 순간인가" (순수 로직).

결정적이다: 같은 (액터, 생활 패턴, 세계 시각, 파라미터) → 같은 답. random·현재
시각 금지 — 리플레이 완전 재현이 존재 조건이다. 날짜·액터별 crc32 해시 분산
(lf_tick.lod.phase_offset 선례)으로 모먼트가 날마다·사람마다 다른 시각에 흩어져
"비정기" 리듬이 된다. 수치(일과 창·하루 목표 수)는 params.yaml이 원천이다.
"""

from __future__ import annotations

import zlib
from datetime import datetime
from functools import cache, lru_cache
from importlib.resources import files
from typing import Any

import yaml

#: 슬롯 폭 = ADR-011 기본 tick의 세계 시간(240초) — 하루 360 슬롯, 시간당 15
WORLD_SECONDS_PER_TICK = 240
SLOTS_PER_DAY = 86_400 // WORLD_SECONDS_PER_TICK
SLOTS_PER_HOUR = 3_600 // WORLD_SECONDS_PER_TICK


@cache
def default_params() -> dict[str, Any]:
    """params.yaml — 리듬 파라미터의 단일 원천 (emotion/goal 선례)."""
    text = (files("lf_actor") / "params.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


def posting_moment(
    actor_id: str, lifestyle: str, world_time: datetime, params: dict[str, Any]
) -> bool:
    """이 세계 시각이 이 액터의 포스팅 모먼트인가.

    평일엔 생활 패턴의 busy 창(일과 시간)에 모먼트가 없고, 주말·휴일엔 창이
    풀리며 하루 목표 수가 늘어난다. 미지의 lifestyle은 flexible로 폴백한다
    (전방 호환 — 새 어휘가 먼저 퍼져도 세계는 멈추지 않는다).
    """
    rhythm = params["rhythm"]
    profiles = rhythm["lifestyles"]
    profile = profiles.get(lifestyle) or profiles["flexible"]
    date = world_time.date()
    # yaml은 따옴표 없는 ISO 날짜를 date로 파싱한다 — str()이 둘 다 ISO 문자열로 편다
    holidays = {str(d) for d in (rhythm.get("holidays") or ())}
    free_day = world_time.weekday() >= 5 or date.isoformat() in holidays
    busy = None if free_day else profile.get("busy")
    open_slots = _open_slots(None if busy is None else (int(busy["start"]), int(busy["end"])))
    target = int(profile["daily_posts"]["weekend" if free_day else "weekday"])
    seconds = world_time.hour * 3_600 + world_time.minute * 60 + world_time.second
    return seconds // WORLD_SECONDS_PER_TICK in _moment_slots(
        actor_id, date.isoformat(), open_slots, target
    )


@cache
def _open_slots(busy: tuple[int, int] | None) -> tuple[int, ...]:
    """busy 창 밖의 하루 슬롯들. start > end면 자정을 넘는 창(night_worker 22~07)."""
    if busy is None:
        return tuple(range(SLOTS_PER_DAY))
    start, end = busy

    def suppressed(hour: int) -> bool:
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    return tuple(s for s in range(SLOTS_PER_DAY) if not suppressed(s // SLOTS_PER_HOUR))


@lru_cache(maxsize=4096)  # (액터, 날짜) 키가 날마다 늘어난다 — 장기 실행 워커 상한
def _moment_slots(
    actor_id: str, day_key: str, open_slots: tuple[int, ...], target: int
) -> frozenset[int]:
    """그 날의 모먼트 슬롯 target개 — (액터, 날짜)별 crc32 분산, 충돌은 이웃으로."""
    chosen: set[int] = set()
    for k in range(min(target, len(open_slots))):
        idx = zlib.crc32(f"{actor_id}:{day_key}:{k}".encode()) % len(open_slots)
        while open_slots[idx] in chosen:
            idx = (idx + 1) % len(open_slots)
        chosen.add(open_slots[idx])
    return frozenset(chosen)
