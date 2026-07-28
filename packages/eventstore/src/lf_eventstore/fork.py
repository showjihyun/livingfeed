"""세계 분기 — 반사실 실험의 정식 도구 (ADR-021 §4 L4).

"내 댓글 하나가 이 세계를 어떻게 바꿨나"에 답하는 방법은 같은 LLM 호출을 다시
재현하려 애쓰는 것(L3, 불가능)이 아니라 **역사를 가르는 것**이다: 분기점까지의
사건을 새 world_id로 복사하고, 그 뒤로 개입만 바꿔 두 갈래를 돌린다. 분기점까지는
L0 보증이 그대로 서고, 그 이후의 발산 자체가 측정 대상이 된다.

## 경계는 tick이 아니라 global_seq다

"tick 500까지"가 사람의 말이지만, tick으로 자르면 틀린다: 플레이어 개입은
tick 0 규약이라(session.py — "순서의 진실은 event_id와 global_seq가 가진다")
tick 필터는 세계의 **모든** 개입을 시점과 무관하게 딸려 온다. 미래의 댓글이
과거의 분기에 들어앉는 셈이다.

그래서 사람의 "tick N까지"를 `system.tick.completed`(tick ≤ N)의 마지막
global_seq로 옮긴 뒤, 그 지점까지의 적재분을 자른다 — "tick N이 끝난 순간까지
세계에 적재된 모든 것". global_seq 접두를 자르므로 스트림별 stream_seq도
자연히 접두가 되어 빈칸이 생기지 않는다.

## 복사하지 않는 것

outbox에는 쓰지 않는다. 분기는 역사의 사본이지 새로 일어난 사건이 아니며,
outbox에 넣으면 전 역사가 JetStream으로 다시 발행되어 살아 있는 프로젝션에
평행 세계가 쏟아진다. 분기 세계의 프로젝션이 필요하면 재구축으로 만든다
(`lf_projector.main --rebuild`, ADR-003 계약 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg import AsyncConnection

from lf_eventstore.model import NewEvent, Provenance
from lf_eventstore.store import append, current_head

logger = logging.getLogger("lf.eventstore.fork")

#: 분기 사실을 남기는 이벤트 — 갈라진 세계가 스스로 그렇다고 말해야 한다
FORK_TYPE = "system.world.forked"
FORK_STREAM = "system"
FORK_STREAM_KEY = "fork"

_BOUNDARY_SQL = """
SELECT max(global_seq) FROM es.events
WHERE world_id = %s AND type = 'system.tick.completed' AND tick <= %s
"""

#: 컬럼을 명시한다 — SELECT *는 컬럼이 늘 때 조용히 어긋난다 (provenance가 그랬듯)
_COPY_SQL = """
INSERT INTO es.events (
    event_id, world_id, stream, stream_key, stream_seq, type,
    schema_version, actor_id, tick, occurred_at, causation_id, correlation_id,
    provenance, payload
)
SELECT event_id, %(target)s, stream, stream_key, stream_seq, type,
       schema_version, actor_id, tick, occurred_at, causation_id, correlation_id,
       provenance, payload
FROM es.events
WHERE world_id = %(source)s AND global_seq <= %(boundary)s
"""

_HEADS_SQL = """
INSERT INTO es.stream_heads (world_id, stream, stream_key, head_seq)
SELECT %(target)s, stream, stream_key, max(stream_seq)
FROM es.events WHERE world_id = %(target)s
GROUP BY stream, stream_key
"""


class ForkRefused(Exception):
    """분기할 수 없다 — 되돌리기 어려운 작업이라 조용히 진행하지 않는다."""


@dataclass(frozen=True)
class ForkResult:
    source_world_id: str
    target_world_id: str
    through_tick: int
    #: 잘라낸 지점 — 이 global_seq까지의 적재분이 사본에 들어갔다
    boundary_global_seq: int
    events_copied: int


async def fork_world(
    conn: AsyncConnection,
    *,
    source_world_id: str,
    target_world_id: str,
    through_tick: int,
    principal: str = "services.gateway",
) -> ForkResult:
    """source의 tick N까지를 target으로 복사한다 (ADR-021 §4 L4).

    target은 비어 있어야 한다 — 기존 세계에 남의 역사를 덧붙이면 두 세계가
    한 스트림에서 섞이고, stream_seq가 겹쳐 낙관적 잠금이 깨진다.
    """
    if source_world_id == target_world_id:
        raise ForkRefused("자기 자신으로 분기할 수 없다")

    cur = await conn.execute(
        "SELECT 1 FROM es.events WHERE world_id = %s LIMIT 1", (target_world_id,)
    )
    if await cur.fetchone() is not None:
        raise ForkRefused(
            f"target 세계 {target_world_id!r}에 이미 역사가 있다 — 빈 세계로만 분기한다"
        )

    cur = await conn.execute(_BOUNDARY_SQL, (source_world_id, through_tick))
    row = await cur.fetchone()
    boundary = None if row is None else row[0]
    if boundary is None:
        raise ForkRefused(
            f"{source_world_id!r}에 tick {through_tick} 이하의 완료된 tick이 없다 —"
            " 분기점을 정할 수 없다 (system.tick.completed가 경계의 원천이다)"
        )

    async with conn.transaction():
        cur = await conn.execute(
            _COPY_SQL,
            {"target": target_world_id, "source": source_world_id, "boundary": boundary},
        )
        copied = cur.rowcount
        await conn.execute(_HEADS_SQL, {"target": target_world_id})

    # 분기 사실을 target의 역사에 남긴다 — 이 기록이 없으면 갈라진 세계가
    # 독립 세계로 오해되고, 두 세계의 비교가 '두 실험'이 아니라 '두 사실'이 된다.
    head = await current_head(conn, target_world_id, FORK_STREAM, FORK_STREAM_KEY)
    await append(
        conn,
        principal,
        [
            NewEvent(
                world_id=target_world_id,
                stream=FORK_STREAM,
                stream_key=FORK_STREAM_KEY,
                type=FORK_TYPE,
                tick=through_tick,
                # 복사는 결정적 작업이다 — 사람이 시작 버튼을 눌렀을 뿐
                # 내용을 지어내지 않았다 (ADR-021 §1)
                provenance=Provenance.derived("eventstore.fork:copy_prefix"),
                payload={
                    "source_world_id": source_world_id,
                    "through_tick": through_tick,
                    "boundary_global_seq": boundary,
                    "events_copied": copied,
                },
            )
        ],
        expected_head=head,
    )

    logger.info(
        "세계 분기: %s → %s (tick %d까지, %d건) — 이 뒤의 발산이 측정 대상이다",
        source_world_id, target_world_id, through_tick, copied,
    )
    return ForkResult(
        source_world_id=source_world_id,
        target_world_id=target_world_id,
        through_tick=through_tick,
        boundary_global_seq=boundary,
        events_copied=copied,
    )
