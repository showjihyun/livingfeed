"""세계를 다시 돌린다 — L0 재생 러너 (ADR-021 §4).

`replay_ai`가 "LLM을 다시 부르지 않는다"를 풀었고, 이 모듈이 그 위에서 세계를
실제로 재생한다. 상태(관계 값·감정·작업 기억)는 **복원하는 것이 아니라 다시
생긴다** — 점진적 복원기가 닿지 못한 곳이 여기서 열린다.

재생 세계는 별도 world_id에 산다: Redis 상태(작업 기억·감정·관계·메일박스)와
이벤트가 전부 세계로 갈려 있어, 새 id를 주는 것만으로 격리가 끝난다. 원본은
읽기만 한다.

## 외부 입력의 재주입

세계가 스스로 만드는 것(행동·응답·감정·관계)은 재생이 다시 만든다. 그러나 밖에서
들어온 것 — 플레이어의 댓글·DM·좋아요 — 은 로그에서 다시 넣어 줘야 한다.

**언제 넣는가가 문제다.** 개입은 tick 0 규약이라(session.py) 봉투의 tick이
지각 시점을 말해 주지 않고, 실제 배달은 라우터가 비동기로 한다. 그래서 지각
시점의 원천을 감정 변화에서 찾는다: `actor.emotion.shifted`의 causation_id가
그 개입을 가리키고 tick이 지각 시점이다 (replay_l1과 같은 단서).

지각 시점을 모르는 개입이 하나라도 있으면 그 시점부터의 재생은 **근사**다.
근사인 채로 대조하면 발산이 '검증 실패'로 오독되므로, 그 tick 이후는 검증
불가로 표시한다 — 이 모듈 전체를 관통하는 규율이다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection

from lf_actor.mailbox import Mailbox
from lf_actor.replay_ai import ReplayAiClient, TracePlayback

logger = logging.getLogger("lf.actor.replay_world")

#: 세계 밖에서 들어오는 것 — 재생이 다시 만들지 못해 다시 넣어 줘야 한다
EXTERNAL_TYPES = ("player.dm.sent", "player.comment.posted", "player.reaction.added")

_EXTERNAL_SQL = """
SELECT global_seq, event_id, stream, type, schema_version, world_id, actor_id, tick,
       occurred_at, causation_id, correlation_id, provenance, payload
FROM es.events
WHERE world_id = %s AND type = ANY(%s)
ORDER BY global_seq
"""

#: 개입이 언제 지각됐는가 — 그 개입이 만든 감정 변화의 tick이 답이다
_PERCEIVED_AT_SQL = """
SELECT causation_id, min(tick) FROM es.events
WHERE world_id = %s AND type = 'actor.emotion.shifted' AND causation_id IS NOT NULL
GROUP BY causation_id
"""

_DECISIONS_SQL = """
SELECT actor_id, tick, payload ->> 'purpose', payload ->> 'bundle_digest'
FROM es.events
WHERE world_id = %s AND type = 'actor.decision.made'
ORDER BY global_seq
"""


class ReplayRefused(Exception):
    """재생을 시작할 수 없다 — 되돌리기 어려운 작업이라 조용히 진행하지 않는다."""


@dataclass(frozen=True)
class ExternalSchedule:
    """언제 무엇을 다시 넣을 것인가."""

    by_tick: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    #: 지각 시점을 모르는 개입들 — 이것이 있으면 재생은 그 시점부터 근사다
    unscheduled: tuple[str, ...] = ()

    @property
    def exact(self) -> bool:
        return not self.unscheduled


async def external_schedule(conn: AsyncConnection, world_id: str) -> ExternalSchedule:
    """개입을 지각 시점별로 묶는다 — 모르는 것은 숨기지 않고 따로 센다."""
    cur = await conn.execute(_PERCEIVED_AT_SQL, (world_id,))
    perceived_at = {cause: tick for cause, tick in await cur.fetchall()}

    cur = await conn.execute(_EXTERNAL_SQL, (world_id, list(EXTERNAL_TYPES)))
    by_tick: dict[int, list[dict[str, Any]]] = {}
    unscheduled: list[str] = []
    for row in await cur.fetchall():
        envelope = {
            "event_id": row[1], "stream": row[2], "type": row[3],
            "schema_version": row[4], "world_id": row[5], "actor_id": row[6],
            "tick": row[7], "occurred_at": row[8].isoformat().replace("+00:00", "Z"),
            "causation_id": row[9], "correlation_id": row[10],
            "provenance": row[11], "payload": row[12],
        }
        tick = perceived_at.get(envelope["event_id"])
        if tick is None:
            # 마음을 흔들지 않은 개입은 흔적이 없다 — 언제 닿았는지 모른다
            unscheduled.append(envelope["event_id"])
            continue
        by_tick.setdefault(tick, []).append(envelope)
    return ExternalSchedule(by_tick=by_tick, unscheduled=tuple(unscheduled))


@dataclass(frozen=True)
class ReplayResult:
    source_world_id: str
    target_world_id: str
    through_tick: int
    ticks_run: int
    #: 소진되지 않은 기록 — 0이 아니면 재생이 원본보다 적게 불렀다 (발산의 신호)
    unused_recordings: int
    schedule_exact: bool


async def replay_world(
    conn: AsyncConnection,
    *,
    source_world_id: str,
    target_world_id: str,
    make_phases: Callable[[ReplayAiClient, str], Any],
    clock: Any,
    redis: Any,
    through_tick: int,
) -> ReplayResult:
    """원본의 기록으로 세계를 다시 돌린다 (ADR-021 §4).

    make_phases는 **그 세계가 쓰던 것과 같은 배선**의 ActorPhases를 만들어야 한다
    (감정·관계 어댑터의 유무가 세계를 바꾼다). 페르소나와 마찬가지로 배선은
    이벤트가 아니므로 로그에서 복원되지 않는다 — 호출자의 몫이다.
    """
    if source_world_id == target_world_id:
        raise ReplayRefused("원본 위에 재생하면 역사가 섞인다 — 새 세계로 돌려라")
    cur = await conn.execute(
        "SELECT 1 FROM es.events WHERE world_id = %s LIMIT 1", (target_world_id,)
    )
    if await cur.fetchone() is not None:
        raise ReplayRefused(f"target 세계 {target_world_id!r}에 이미 역사가 있다")

    playback = await TracePlayback.load(conn, source_world_id, through_tick=through_tick)
    schedule = await external_schedule(conn, source_world_id)
    mailbox = Mailbox(redis)
    phases = make_phases(ReplayAiClient(playback), target_world_id)

    head = 0
    for tick in range(through_tick + 1):
        for envelope in schedule.by_tick.get(tick, []):
            target = envelope["payload"].get("target_actor_id")
            if target:
                # 재생 세계의 메일박스로 — 원본은 읽기만 한다
                await mailbox.push(target_world_id, target, envelope)
        head = await run_tick(
            conn, phases, clock, target_world_id, tick, head,
            # 재생 세계의 주민은 자기가 원본에 산다고 안다 — world_id는 기록용
            # 라벨이라, 갈라 두지 않으면 컨텍스트가 라벨 하나 때문에 어긋난다
            perceived_world_id=source_world_id,
        )

    logger.info(
        "재생 완료: %s → %s (tick %d까지) — 미소진 기록 %d건, 일정 %s",
        source_world_id, target_world_id, through_tick, playback.remaining,
        "정확" if schedule.exact else "근사",
    )
    return ReplayResult(
        source_world_id=source_world_id,
        target_world_id=target_world_id,
        through_tick=through_tick,
        ticks_run=through_tick + 1,
        unused_recordings=playback.remaining,
        schedule_exact=schedule.exact,
    )


@dataclass(frozen=True)
class DecisionDiff:
    """같은 자리의 두 결정 — 원본과 재생."""

    actor_id: str
    tick: int
    purpose: str
    source_digest: str
    replay_digest: str

    @property
    def matches(self) -> bool:
        return self.source_digest == self.replay_digest


@dataclass
class ReplayComparison:
    """두 세계의 결정 대조 — L1을 리플레이 위에서 세운 결과.

    `ok`는 어긋남이 없다는 뜻이지 '전부 검증됐다'가 아니다 (replay_l1과 같은 규율).
    일정이 근사이거나 결정 수가 다르면 대조 자체가 성립하지 않는다.
    """

    diffs: list[DecisionDiff] = field(default_factory=list)
    #: 대조가 성립하지 않는 이유 — 있으면 어긋남을 보고하지 않는다
    unverifiable: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.unverifiable and all(d.matches for d in self.diffs)

    @property
    def mismatched(self) -> list[DecisionDiff]:
        return [] if self.unverifiable else [d for d in self.diffs if not d.matches]

    def summary(self) -> dict[str, Any]:
        return {
            "decisions": len(self.diffs),
            "verified": 0 if self.unverifiable else sum(1 for d in self.diffs if d.matches),
            "mismatched": len(self.mismatched),
            "unverifiable": list(self.unverifiable),
        }


async def compare_decisions(
    conn: AsyncConnection, source_world_id: str, target_world_id: str,
    *, schedule_exact: bool = True, unused_recordings: int = 0,
) -> ReplayComparison:
    """원본과 재생의 결정을 짝지어 대조한다.

    훅이 필요 없다: 재생 세계도 자기 actor.decision.made를 낸다. 두 세계의 결정을
    순서대로 짝지어 bundle_digest를 맞추면, 재생이 같은 컨텍스트에 닿았는지가
    그대로 드러난다.
    """
    reasons: list[str] = []
    if not schedule_exact:
        reasons.append("external_schedule")  # 개입의 지각 시점을 몰라 근사 재생이다
    if unused_recordings:
        reasons.append("unused_recordings")  # 재생이 원본보다 적게 불렀다

    rows: dict[str, list[tuple[str, int, str, str]]] = {}
    for world in (source_world_id, target_world_id):
        cur = await conn.execute(_DECISIONS_SQL, (world,))
        rows[world] = list(await cur.fetchall())

    source, replay = rows[source_world_id], rows[target_world_id]
    if len(source) != len(replay):
        reasons.append("decision_count")  # 결정 수가 다르면 짝짓기가 성립하지 않는다

    diffs = [
        DecisionDiff(
            actor_id=s[0], tick=s[1], purpose=s[2],
            source_digest=s[3], replay_digest=r[3],
        )
        for s, r in zip(source, replay, strict=False)
        if s[0] == r[0] and s[1] == r[1] and s[2] == r[2]
    ]
    if len(diffs) != min(len(source), len(replay)):
        reasons.append("decision_alignment")  # 자리가 어긋났다 — 대조 대상이 아니다

    return ReplayComparison(diffs=diffs, unverifiable=tuple(sorted(set(reasons))))


def _import_run_tick() -> Any:
    from lf_tick.engine import run_tick as _run_tick

    return _run_tick


#: lf_tick은 lf_actor를 import하지 않지만, 여기서 최상위 import하면 테스트가
#: 엔진 패키지를 항상 끌고 온다 — 호출 시점에 묶는다 (phases.py의 선례).
run_tick: Callable[..., Any] = _import_run_tick()


__all__: Sequence[str] = (
    "DecisionDiff",
    "ExternalSchedule",
    "ReplayComparison",
    "ReplayRefused",
    "ReplayResult",
    "compare_decisions",
    "external_schedule",
    "replay_world",
)
