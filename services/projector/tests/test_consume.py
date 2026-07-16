"""공용 소비 루프 검증 (ADR-003) — 폴링 지속·once 드레인 종료의 경계.

once는 --rebuild를 일회성 배치로 만드는 열쇠다: 스트림이 유휴(fetch 타임아웃)해질
때까지 소비하고 스스로 끝난다. 상시 모드는 타임아웃을 조용히 지나쳐 계속 돈다.
"""

import asyncio

import nats.errors
from lf_projector.consume import batches


class ScriptedSub:
    """스크립트된 pull consumer 스텁 — 배치 목록 또는 예외를 차례로 돌려준다."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls = 0

    async def fetch(self, batch: int, timeout: float):  # noqa: ASYNC109 — nats psub.fetch 시그니처 미러
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def _collect(psub, *, stop: asyncio.Event, once: bool) -> list:
    got = []
    async for msgs in batches(psub, batch_size=8, timeout_s=0.1, stop=stop, once=once):
        got.append(msgs)
    return got


async def test_once_drains_then_ends_on_idle():
    psub = ScriptedSub([["m1"], ["m2"], TimeoutError()])
    got = await _collect(psub, stop=asyncio.Event(), once=True)
    assert got == [["m1"], ["m2"]]  # 유휴까지 전부 소비하고 스스로 끝난다
    assert psub.calls == 3


async def test_once_treats_nats_timeout_as_idle():
    # nats-py fetch는 경로에 따라 asyncio.TimeoutError 대신 자기 타임아웃을 던진다
    psub = ScriptedSub([nats.errors.TimeoutError()])
    assert await _collect(psub, stop=asyncio.Event(), once=True) == []
    assert psub.calls == 1


async def test_steady_mode_polls_through_idle():
    stop = asyncio.Event()
    psub = ScriptedSub([["m1"], TimeoutError(), nats.errors.TimeoutError(), ["m2"]])
    got = []
    async for msgs in batches(psub, batch_size=8, timeout_s=0.1, stop=stop, once=False):
        got.append(msgs)
        if msgs == ["m2"]:
            stop.set()  # 상시 모드의 유일한 출구는 stop이다
    assert got == [["m1"], ["m2"]]  # 타임아웃 두 번을 지나쳐 계속 돌았다
    assert psub.calls == 4


async def test_stop_preempts_fetch():
    stop = asyncio.Event()
    stop.set()
    psub = ScriptedSub([["never"]])
    assert await _collect(psub, stop=stop, once=False) == []
    assert psub.calls == 0  # stop이 이미 서 있으면 fetch에 들어가지 않는다
