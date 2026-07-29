"""L1 러너 — 결정 시점의 컨텍스트를 다시 조립해 대조한다 (ADR-021 §4 L1).

`bundle_digest`는 도구일 뿐이고, 이 모듈이 그 도구를 실제로 돌리는 곳이다:
세계의 `actor.decision.made`를 훑으며 그때의 번들을 다시 조립하고 지문을 맞춰
본다. 맞으면 **그 인물이 그 순간 무엇을 알고 있었는지가 증명된다** — LLM 출력을
재현하지 못해도(§4 L3) 입력은 재현할 수 있다는 것이 연구용 관측성의 실질이다.

## 복원 못 한 입력은 사고가 아니다

가장 조심할 것은 거짓 사고다. 재조립 입력을 다 복원하지 못한 채 지문을 맞춰
보면 당연히 어긋나는데, 그것을 '검증 실패'로 보고하면 **없는 회귀를 만든다**
(ADR-021 §결과의 경고와 같은 결). 그래서 판정은 세 갈래이며, 입력이 불완전하면
어긋나도 MISMATCH가 아니라 UNVERIFIABLE이다 — 무엇을 복원하지 못했는지를
이름으로 달고서.

MISMATCH는 **모든 입력을 복원한 상태에서만** 나온다. 그때는 진짜 사고다:
조립이 순수하지 않거나, 입력이 조용히 달라졌거나, 이벤트가 손상됐다.

## 지금 복원되는 것

resolver가 이벤트 로그(+ 호출자가 준 페르소나 명부)에서 복원한다:

    identity     페르소나 — 저작물이라 호출자가 그 세계가 쓰던 원천을 준다
    world        tick과 시계에서 (결정적)
    task_frame   purpose가 기록에 있다
    episodes     sections[episodes].source_ids → actor.memory.consolidated 본문
                 (회상은 Qdrant의 decay_at 때문에 재실행이 비결정적이다. 그래서
                  source_ids를 남겼다 — 다시 검색하지 않고 그때 들어간 것을 집는다)
    arc          system.director.arc_planned의 접기

복원되지 않는 것: working(작업 기억), relationships, conversation, seen_posts.
전부 Redis 상태이거나 그 파생이라, 온전한 복원은 세계를 그 tick까지 다시 돌리는
L0 리플레이 위에서만 성립한다. 예외로 **액터의 첫 결정**은 작업 기억이 비어
있음이 증명되므로(그 이전 이벤트가 없다) 완전 복원이 가능하다 — 세계의 개막이
통째로 검증되는 것은 그 자체로 값지고, 커버리지를 넓히는 발판이다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from lf_eventstore.tiers import ReplayTier, assert_verifiable
from psycopg import AsyncConnection

from lf_actor.arc import Arc
from lf_actor.cognition import DEFAULT_MEMORY_TOKENS
from lf_actor.context import DigestVerdict, WorldContext, build, verify_digest
from lf_actor.persona import Persona
from lf_actor.semantic import Recollection

logger = logging.getLogger("lf.actor.replay_l1")

#: 아직 이벤트에서 복원하지 못하는 섹션 (전부 Redis 상태이거나 그 파생)
_UNRESOLVABLE_SECTIONS = frozenset({"relationships", "conversation", "seen_posts"})

_DECISIONS_SQL = """
SELECT global_seq, event_id, actor_id, tick, payload
FROM es.events
WHERE world_id = %s AND type = 'actor.decision.made'
  AND (%s::bigint IS NULL OR tick <= %s)
ORDER BY global_seq
"""

_EPISODE_SQL = """
SELECT event_id, payload ->> 'summary'
FROM es.events
WHERE world_id = %s AND type = 'actor.memory.consolidated' AND event_id = ANY(%s)
"""

_ARC_SQL = """
SELECT payload ->> 'stage', payload ->> 'intention'
FROM es.events
WHERE world_id = %s AND type = 'system.director.arc_planned'
  AND payload ->> 'target_actor_id' = %s AND tick <= %s
ORDER BY global_seq DESC LIMIT 1
"""

#: 이 액터가 이 지점 이전에 남긴 것이 있는가 — 작업 기억이 비었음의 증명
_PRIOR_SQL = """
SELECT 1 FROM es.events
WHERE world_id = %s AND actor_id = %s AND global_seq < %s
  AND type <> 'actor.decision.made'
LIMIT 1
"""


class L1Verdict(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class DecisionCheck:
    """결정 하나의 대조 결과."""

    event_id: str
    actor_id: str
    tick: int
    verdict: L1Verdict
    #: UNVERIFIABLE의 이유 — 무엇을 복원하지 못했는지가 곧 커버리지의 다음 과제다
    unresolved: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.verdict is L1Verdict.MATCH


@dataclass
class L1Report:
    """세계 하나의 L1 리포트.

    `ok`는 **어긋남이 없다**는 뜻이지 '전부 검증됐다'가 아니다 — 검증하지 못한
    것을 통과로 세면 리포트가 거짓말을 한다. 두 수를 따로 낸다.
    """

    world_id: str
    checks: list[DecisionCheck] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def verified(self) -> int:
        return sum(1 for c in self.checks if c.verdict is L1Verdict.MATCH)

    @property
    def mismatched(self) -> list[DecisionCheck]:
        return [c for c in self.checks if c.verdict is L1Verdict.MISMATCH]

    @property
    def unverifiable(self) -> list[DecisionCheck]:
        return [c for c in self.checks if c.verdict is L1Verdict.UNVERIFIABLE]

    @property
    def ok(self) -> bool:
        """어긋남이 하나도 없다 — 검증 불가는 실패가 아니다 (ADR-021 §4)."""
        return not self.mismatched

    def summary(self) -> dict[str, Any]:
        blockers: dict[str, int] = {}
        for check in self.unverifiable:
            for name in check.unresolved:
                blockers[name] = blockers.get(name, 0) + 1
        return {
            "world_id": self.world_id,
            "ok": self.ok,
            "decisions": self.total,
            "verified": self.verified,
            "mismatched": len(self.mismatched),
            "unverifiable": len(self.unverifiable),
            # 무엇이 커버리지를 막고 있는가 — 다음에 무엇을 복원해야 하는지의 목록
            "blocked_by": dict(sorted(blockers.items())),
        }


async def _resolve_episodes(
    conn: AsyncConnection, world_id: str, source_ids: list[str]
) -> list[Recollection] | None:
    """회상 섹션의 재료 — source_ids가 가리키는 응고 기억의 본문.

    하나라도 못 찾으면 None이다: 일부만 복원한 회상으로 조립하면 지문이 어긋나고,
    그 어긋남은 사고가 아니라 우리 복원의 구멍이다.
    """
    if not source_ids:
        return []
    cur = await conn.execute(_EPISODE_SQL, (world_id, source_ids))
    found = {event_id: summary for event_id, summary in await cur.fetchall()}
    if len(found) != len(set(source_ids)):
        return None
    # 기록된 순서 그대로 — 조립 순서가 지문의 일부다
    return [Recollection(event_id=eid, text=found[eid]) for eid in source_ids]


async def _resolve_arc(
    conn: AsyncConnection, world_id: str, actor_id: str, tick: int
) -> Arc | None:
    cur = await conn.execute(_ARC_SQL, (world_id, actor_id, tick))
    row = await cur.fetchone()
    return None if row is None else Arc(stage=row[0], intention=row[1])


async def _working_is_provably_empty(
    conn: AsyncConnection, world_id: str, actor_id: str, global_seq: int
) -> bool:
    """이 결정 이전에 이 액터가 남긴 것이 없는가 — 있으면 작업 기억을 복원 못 한다."""
    cur = await conn.execute(_PRIOR_SQL, (world_id, actor_id, global_seq))
    return await cur.fetchone() is None


async def check_decision(
    conn: AsyncConnection,
    world_id: str,
    row: tuple[int, str, str, int, dict[str, Any]],
    personas: dict[str, Persona],
    world_time_at: Any,
) -> DecisionCheck:
    """결정 하나를 재조립해 대조한다 — 입력을 다 못 채우면 대조하지 않는다."""
    global_seq, event_id, actor_id, tick, payload = row
    unresolved: list[str] = []

    persona = personas.get(actor_id)
    if persona is None:
        unresolved.append("persona")

    sections = {s["kind"]: s for s in payload.get("sections", [])}
    unresolved += sorted(_UNRESOLVABLE_SECTIONS & set(sections))

    episodes: list[Recollection] | None = []
    if "episodes" in sections:
        episodes = await _resolve_episodes(
            conn, world_id, list(sections["episodes"].get("source_ids") or [])
        )
        if episodes is None:
            unresolved.append("episodes")

    if not await _working_is_provably_empty(conn, world_id, actor_id, global_seq):
        # 작업 기억은 Redis 상태다 — 온전한 복원은 L0 리플레이 위에서만 성립한다
        unresolved.append("working")

    if unresolved:
        return DecisionCheck(
            event_id=event_id, actor_id=actor_id, tick=tick,
            verdict=L1Verdict.UNVERIFIABLE, unresolved=tuple(sorted(set(unresolved))),
        )

    assert persona is not None
    rebuilt = build(
        persona,
        [],  # 첫 결정 — 작업 기억이 비어 있음이 증명됐다
        WorldContext(world_id=world_id, tick=tick, world_time=world_time_at(tick)),
        purpose=payload["purpose"],
        episodes=episodes,
        arc=await _resolve_arc(conn, world_id, actor_id, tick),
        # 그때 적용된 인지 예산으로 조립한다 (ADR-021 §3) — 예산이 다르면 다른
        # 컨텍스트이므로, 지금 설정이 아니라 기록된 값을 써야 대조가 성립한다
        memory_tokens=(payload.get("cognitive_budget") or {}).get("memory_tokens")
        or DEFAULT_MEMORY_TOKENS,
        trace_id="재조립은 trace_id에 영향받지 않는다",
    )
    digest_verdict = verify_digest(payload["bundle_digest"], rebuilt.digest)
    if digest_verdict is DigestVerdict.UNVERIFIABLE:
        # 조립기 버전이 다르다 — 실패가 아니라 알 수 없음이다 (ADR-021 §2)
        return DecisionCheck(
            event_id=event_id, actor_id=actor_id, tick=tick,
            verdict=L1Verdict.UNVERIFIABLE, unresolved=("assembler_version",),
        )
    return DecisionCheck(
        event_id=event_id, actor_id=actor_id, tick=tick,
        verdict=L1Verdict.MATCH
        if digest_verdict is DigestVerdict.MATCH
        else L1Verdict.MISMATCH,
    )


async def verify_world(
    conn: AsyncConnection,
    world_id: str,
    personas: Iterable[Persona],
    *,
    world_time_at: Any,
    through_tick: int | None = None,
) -> L1Report:
    """세계의 결정들을 재조립해 대조한다 (ADR-021 §4 L1).

    personas는 그 세계가 쓰던 것과 같은 원천이어야 한다 — 저작물은 이벤트가
    아니므로 복원되지 않는다 (L2와 같은 계약, replay_rules 참고).
    world_time_at은 tick → 세계 시간 순수 함수다 (lf_tick.clock.TickClock).
    """
    assert_verifiable(ReplayTier.REASSEMBLY)

    roster = {p.id: p for p in personas}
    cur = await conn.execute(_DECISIONS_SQL, (world_id, through_tick, through_tick))
    report = L1Report(world_id=world_id)
    for row in await cur.fetchall():
        report.checks.append(
            await check_decision(conn, world_id, row, roster, world_time_at)
        )

    summary = report.summary()
    logger.info(
        "L1 재조립 %s — world=%s 검증 %d/%d, 어긋남 %d, 검증 불가 %d %s",
        "OK" if report.ok else "MISMATCH", world_id, summary["verified"],
        summary["decisions"], summary["mismatched"], summary["unverifiable"],
        summary["blocked_by"],
    )
    return report
