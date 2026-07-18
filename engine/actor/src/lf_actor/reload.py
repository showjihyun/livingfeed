"""페르소나 핫 리로드 — 파일(SoT)의 변경이 재시작 없이 세계에 반영된다.

지문은 (yaml 파일 이름 목록, max mtime) — 추가·제거·수정 어느 것이든 지문을
바꾼다. 감지·리로드는 tick 경계(schedule 직전)에서만 일어난다 — tick 중간의
명부 변경은 금지다 (파이프라인 단계 간 일관성). 편집 중 깨진 yaml은 tick을
멈추지 않는다 — 기존 명부를 유지하고, 저장이 끝나면(mtime 전진) 다음 경계에서
다시 시도한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from lf_tick.pipeline import TickContext

from lf_actor.persona import Persona, load_personas
from lf_actor.phases import ActorPhases

logger = logging.getLogger("lf.actor.reload")

#: 디렉터리 지문 — (정렬된 파일 이름들, 최댓값 mtime_ns)
Fingerprint = tuple[tuple[str, ...], int]


def scan_fingerprint(directory: Path) -> Fingerprint:
    """personas 디렉터리의 변경 지문 (순수 — 파일 목록 + max mtime)."""
    files = sorted(directory.glob("*.yaml"))
    names = tuple(p.name for p in files)
    max_mtime = max((p.stat().st_mtime_ns for p in files), default=0)
    return names, max_mtime


class PersonaReloader:
    """지문이 바뀌었을 때만 load_personas를 다시 태우는 감지기.

    poll이 지문을 먼저 굳히고 읽는다 — 읽기가 실패해도(편집 중 깨진 yaml)
    같은 지문으로 재시도하며 로그를 도배하지 않는다. 고친 저장은 mtime을
    다시 전진시키므로 다음 poll이 잡는다.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._fingerprint: Fingerprint | None = None

    def load(self) -> list[Persona]:
        """최초 로드 — 지문을 굳히고 전체를 읽는다 (워커 시작 시 1회).

        샤드 분할(ADR-012 Phase 2)은 여기가 아니라 ActorPhases의 몫이다 —
        실행 집합은 샤드별이지만 이름·유효 대상·팬아웃 명부는 세계 전체다.
        """
        self._fingerprint = scan_fingerprint(self._dir)
        return load_personas(self._dir)

    def poll(self) -> list[Persona] | None:
        """변경이 있으면 새 명부, 없으면 None. 읽기 실패는 호출자에게 던진다."""
        fingerprint = scan_fingerprint(self._dir)
        if fingerprint == self._fingerprint:
            return None
        self._fingerprint = fingerprint
        return load_personas(self._dir)


class ReloadingPhases:
    """ActorPhases 래퍼 — 매 tick의 첫 관문(schedule) 직전에 리로드를 반영한다.

    run_tick은 schedule→world→perceive→decide→resolve→consolidate 순서로
    부른다 — schedule 직전이 곧 tick 경계다. 새 액터는 이번 tick의 scheduled에
    바로 실리고, 같은 tick의 world()가 정체성을 선언한다 (데뷔의 손맛).
    on_reload(새 명부)로 바깥 협력자(FeedFanout 명부 등)도 따라온다.
    """

    def __init__(
        self,
        phases: ActorPhases,
        reloader: PersonaReloader,
        *,
        on_reload: Callable[[list[Persona]], None] | None = None,
    ) -> None:
        self._phases = phases
        self._reloader = reloader
        self._on_reload = on_reload

    async def schedule(self, ctx: TickContext) -> dict[str, int]:
        self._maybe_reload(ctx.tick)
        return await self._phases.schedule(ctx)

    def _maybe_reload(self, tick: int) -> None:
        try:
            personas = self._reloader.poll()
        except Exception as e:  # 편집 중 깨진 yaml 등 — 세계는 멈추지 않는다
            logger.warning("페르소나 리로드 실패(기존 명부 유지): %s", e)
            return
        if personas is None:
            return
        if not personas:
            logger.warning("페르소나 리로드 무시: 활성 페르소나 0명 (기존 명부 유지)")
            return
        added, removed = self._phases.refresh_personas(personas, tick=tick)
        if added or removed:
            logger.info("페르소나 리로드: +%d -%d (%s)", len(added), len(removed),
                        ", ".join([*("+" + a for a in added), *("-" + r for r in removed)]))
        if self._on_reload is not None:
            self._on_reload(personas)

    async def world(self, ctx: TickContext) -> None:
        await self._phases.world(ctx)

    async def perceive(self, ctx: TickContext) -> None:
        await self._phases.perceive(ctx)

    async def decide(self, ctx: TickContext) -> dict[str, int]:
        return await self._phases.decide(ctx)

    async def resolve(self, ctx: TickContext) -> int:
        return await self._phases.resolve(ctx)

    async def consolidate(self, ctx: TickContext) -> None:
        await self._phases.consolidate(ctx)
