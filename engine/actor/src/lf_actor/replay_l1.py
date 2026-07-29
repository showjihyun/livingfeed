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

    working      자기 기록(행동·응답·곱씹음)의 접기. 문장은 엔진과 **같은 함수**로
                 만든다 (memo.py) — 포맷을 두 곳에서 쓰면 갈리는 순간 러너가
                 없는 회귀를 보고한다. 지각이 없었던 구간에서만 완결된다:
                 배달된 봉투의 서술과 감정 줄은 배달 이력·감정 상태에 달려 있다.
                 배달 판정은 mailbox.route_targets가 유일한 지점이고 네 규칙 중
                 셋이 봉투만으로 닫히지만, 피드 팬아웃은 관계 상태를 봐야 해서
                 피드 포스트가 하나라도 있으면 단정하지 않는다.

복원되지 않는 것: relationships, conversation, seen_posts (전부 Redis 상태이거나
그 파생). 그 섹션이 기록에 있으면 그 결정은 검증 불가다.

## 가정이 깨질 때

위 배달 판정은 "지각 봉투는 이 세계의 이벤트 로그에 있다"에 기댄다 (라우터가
JetStream에서 받고, 그건 outbox를 거친 것이므로 운영에선 참이다). 그러나 가정은
언젠가 깨지고, 깨진 채 대조를 강행하면 거짓 회귀가 쏟아진다. 그래서 대조 **전에**
섹션별 토큰 수를 기록과 맞춰 본다: 어긋나면 우리 복원이 틀린 것이므로 그 섹션을
이름으로 달고 검증 불가로 떨어진다. 일치는 '복원이 믿을 만하다'의 필요 조건이고,
내용의 동일성은 그다음 digest가 판정한다.
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
from lf_actor.memo import memo_for_own_event
from lf_actor.memory import DEFAULT_MAX_ENTRIES
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

#: 이 액터가 이 지점 이전에 남긴 것들 — 작업 기억의 자기 기록 부분 (memo.py)
_OWN_SQL = """
SELECT type, tick, payload FROM es.events
WHERE world_id = %s AND actor_id = %s AND global_seq < %s
  AND type IN ('actor.action.performed', 'actor.message.sent', 'actor.belief.formed')
ORDER BY global_seq
"""

#: 이 지점 이전에 **이 액터에게 배달됐을** 봉투가 있는가.
#: mailbox.route_targets가 배달 판단의 유일한 지점이고, 네 규칙 중 셋은 봉투만으로
#: 닫힌다 — 대상 1명(payload.target_actor_id), 액터 댓글(comment_targets),
#: 세계 사건(affected_actor_ids). 남는 하나가 피드 팬아웃인데 관계 상태를 봐야 해서
#: 로그만으로는 대상을 알 수 없다: 그래서 feed.post.published가 하나라도 있으면
#: 배달 여부를 단정하지 못한다(아래 별도 질의).
_DELIVERED_SQL = """
SELECT 1 FROM es.events
WHERE world_id = %s AND global_seq < %s AND (
      payload ->> 'target_actor_id' = %s
   OR (type = 'world.incident.occurred'
       AND payload -> 'affected_actor_ids' ? %s)
)
LIMIT 1
"""

_ANY_FEED_SQL = """
SELECT 1 FROM es.events
WHERE world_id = %s AND global_seq < %s AND type = 'feed.post.published'
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


async def _perceived_anything(
    conn: AsyncConnection, world_id: str, actor_id: str, global_seq: int
) -> bool:
    """이 지점 이전에 이 액터에게 배달됐을 봉투가 있는가 (또는 알 수 없는가).

    지각이 있었다면 작업 기억에 describe_interaction 줄과 mood_line이 들어갔고,
    그 둘은 각각 배달 이력과 감정 상태에 달려 있어 로그만으로 복원되지 않는다.
    피드 포스트가 하나라도 있으면 팬아웃 대상을 알 수 없어 "없었다"를 단정할 수
    없다 — 모르는 것을 없다고 세면 복원이 조용히 틀린다.
    """
    cur = await conn.execute(_DELIVERED_SQL, (world_id, global_seq, actor_id, actor_id))
    if await cur.fetchone() is not None:
        return True
    cur = await conn.execute(_ANY_FEED_SQL, (world_id, global_seq))
    return await cur.fetchone() is not None


async def _rebuild_working(
    conn: AsyncConnection, world_id: str, actor_id: str, global_seq: int,
    roster: dict[str, str],
) -> list[str]:
    """자기 기록만으로 작업 기억을 되짚는다 (지각이 없었음이 확인된 경우).

    엔진과 **같은 함수**로 문장을 만든다 (memo.py) — 포맷을 여기서 다시 쓰면
    갈리는 순간 러너가 없는 회귀를 보고한다. 최신 우선·상한 절단도 WorkingMemory의
    쓰기 규칙(LPUSH + LTRIM)과 같아야 같은 컨텍스트가 나온다.
    """
    cur = await conn.execute(_OWN_SQL, (world_id, actor_id, global_seq))
    entries: list[str] = []
    for kind, tick, payload in await cur.fetchall():
        memo = memo_for_own_event(
            {"type": kind, "tick": tick, "payload": payload}, roster
        )
        if memo is not None:
            entries.append(memo)
    entries.reverse()  # 최신 우선 (LPUSH와 같은 순서)
    return entries[:DEFAULT_MAX_ENTRIES]


def _sections_out_of_step(
    recorded: dict[str, dict[str, Any]], rebuilt: Any
) -> set[str]:
    """재구성이 기록과 다른 섹션들 — 크기가 다르면 내용도 다르다.

    토큰 수 일치는 '복원이 믿을 만하다'의 **필요 조건**이다. 통과해도 내용이
    같음을 보장하지는 않지만(그건 digest의 몫), 실패하면 우리 복원이 틀렸다는
    것만은 확실하다. 그때 대조를 강행해 어긋남을 보고하면 없는 회귀가 된다.
    """
    made = {s.kind: s for s in rebuilt.sections}
    out_of_step = set(recorded) ^ set(made)  # 한쪽에만 있는 섹션
    for kind, section in recorded.items():
        mine = made.get(kind)
        if mine is not None and mine.token_count != section.get("token_count"):
            out_of_step.add(kind)
    return out_of_step


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

    # 작업 기억의 자기 기록 부분은 로그에서 접을 수 있다 (memo.py). 지각이 있었다면
    # describe_interaction 줄과 mood_line이 섞이는데 그 둘은 배달 이력·감정 상태에
    # 달려 있어 복원되지 않는다 — 그때만 포기한다.
    working: list[str] = []
    if await _perceived_anything(conn, world_id, actor_id, global_seq):
        unresolved.append("working")
    else:
        working = await _rebuild_working(
            conn, world_id, actor_id, global_seq,
            {pid: p.name for pid, p in personas.items()},
        )

    if unresolved:
        return DecisionCheck(
            event_id=event_id, actor_id=actor_id, tick=tick,
            verdict=L1Verdict.UNVERIFIABLE, unresolved=tuple(sorted(set(unresolved))),
        )

    assert persona is not None
    rebuilt = build(
        persona,
        working,
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
    # 복원이 믿을 만한가를 먼저 본다 — 섹션별 토큰 수는 기록에 있다.
    # 어긋나면 그 섹션의 재구성이 틀린 것이지 세계가 어긋난 것이 아니다.
    # 이 가드가 없으면 러너의 구멍이 전부 '사고'로 보고된다 (지각 봉투가
    # 로그 밖에서 배달되는 경우처럼, 우리 가정이 깨지는 순간이 반드시 온다).
    drifted = tuple(sorted(_sections_out_of_step(sections, rebuilt)))
    if drifted:
        return DecisionCheck(
            event_id=event_id, actor_id=actor_id, tick=tick,
            verdict=L1Verdict.UNVERIFIABLE, unresolved=drifted,
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
