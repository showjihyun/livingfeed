"""공용 소비 루프 — 네 프로젝터가 공유하는 pull 폴링의 한 가지 모양 (ADR-003).

once=True가 --rebuild를 일회성 배치로 만든다: durable을 지우고 처음부터
재소비하다가 스트림이 유휴(fetch 타임아웃)해지면 스스로 끝난다 — 주간
verify 배치가 어긋남을 찾았을 때 `--rebuild --once` 한 방이 복구다.
상시 모드(once=False)는 타임아웃을 조용히 지나쳐 stop까지 돈다.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import nats.errors


async def batches(
    psub: Any, *, batch_size: int, timeout_s: float, stop: asyncio.Event, once: bool = False
) -> AsyncIterator[list[Any]]:
    """pull consumer의 배치 반복 — stop까지 폴링, once면 첫 유휴에서 끝난다."""
    while not stop.is_set():
        try:
            yield await psub.fetch(batch_size, timeout=timeout_s)
        except (TimeoutError, nats.errors.TimeoutError):
            # nats-py fetch는 경로에 따라 asyncio.TimeoutError도 던진다
            if once:
                return
