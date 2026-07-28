"""결정 트레이스 저장 — 프롬프트 원문과 그 보존 정책 (ADR-021 §2/§5).

트레이스는 **이벤트가 아니다**. 이벤트는 세계의 역사라 영구 보존이고, 트레이스는
"왜 그렇게 결정했나"를 사후에 읽기 위한 소모품이다. 수명이 다르므로 표가 다르고,
이벤트는 trace_id로 이 표를 참조만 한다 (마이그레이션 0005의 근거).

두 가지를 의도적으로 어렵게 만들어 두었다:

1. **샘플링은 결정적이다.** 난수로 고르면 같은 세계를 리플레이할 때마다 다른
   트레이스가 남아, "그때 왜 이 결정만 기록이 없지?"를 영원히 답할 수 없다.
   trace_id 해시로 고르면 같은 결정은 언제나 같은 판정을 받는다.
2. **기록 여부가 이벤트에 남는다.** actor.decision.made.trace_retained가 그것이다.
   이 값이 없으면 조회가 비었을 때 "샘플링에서 빠졌다"와 "유실됐다"를 구분할 수
   없고, 없는 사고를 쫓거나 진짜 사고를 놓치게 된다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import AsyncConnection

#: 기본 모드 — 운영 디버깅용. 1%만, 7일 (ADR-021 §5 표)
DEFAULT_SAMPLE_RATE = 0.01
DEFAULT_RETENTION = timedelta(days=7)

#: 연구 모드 — 재현 실험용. 전량, 30일
RESEARCH_SAMPLE_RATE = 1.0
RESEARCH_RETENTION = timedelta(days=30)

#: 결정적 샘플링의 분해능 — trace_id 해시를 이 범위의 정수로 접는다
_SAMPLE_BUCKETS = 10_000


@dataclass(frozen=True)
class TracePolicy:
    """세계 하나의 트레이스 보존 정책.

    research 모드는 세계 단위 명시적 옵트인이며, 켜져 있다는 사실 자체가
    system 이벤트로 남아야 한다 (ADR-021 §5) — 어떤 기간의 데이터가 연구
    등급인지는 데이터 안에서 확인 가능해야 하기 때문이다. 이 클래스는
    정책의 값만 안다; 모드 전환을 이벤트로 남기는 것은 호출자의 몫이다.
    """

    sample_rate: float = DEFAULT_SAMPLE_RATE
    retention: timedelta = DEFAULT_RETENTION

    @classmethod
    def research(cls) -> TracePolicy:
        return cls(sample_rate=RESEARCH_SAMPLE_RATE, retention=RESEARCH_RETENTION)

    @classmethod
    def for_mode(cls, *, research: bool) -> TracePolicy:
        return cls.research() if research else cls()

    def retains(self, trace_id: str) -> bool:
        """이 결정의 원문을 남길 것인가 — 같은 trace_id는 언제나 같은 답.

        경계를 양끝에서 닫는다: rate 0이면 무엇도 남기지 않고, 1이면 무엇도
        빠뜨리지 않는다. 해시 기반이라 그 사이 값은 세계·시간과 무관하게
        고르게 흩어지며, 리플레이해도 같은 집합이 남는다.
        """
        if self.sample_rate <= 0:
            return False
        if self.sample_rate >= 1:
            return True
        digest = hashlib.sha256(trace_id.encode()).digest()
        bucket = int.from_bytes(digest[:4], "big") % _SAMPLE_BUCKETS
        return bucket < self.sample_rate * _SAMPLE_BUCKETS


@dataclass(frozen=True)
class DecisionTrace:
    """저장 단위 — 결정 한 건의 입력과 출력 원문."""

    trace_id: str
    world_id: str
    tick: int
    purpose: str
    system_prompt: str
    user_prompt: str
    actor_id: str | None = None
    output: str | None = None
    model: str | None = None


_INSERT = """
INSERT INTO es.decision_traces
    (trace_id, world_id, actor_id, tick, purpose,
     system_prompt, user_prompt, output, model, expires_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trace_id) DO NOTHING
"""


async def store_trace(
    conn: AsyncConnection,
    trace: DecisionTrace,
    policy: TracePolicy,
    *,
    now: datetime | None = None,
) -> bool:
    """정책이 허락하면 원문을 남긴다. 반환값이 곧 trace_retained다.

    같은 trace_id의 재적재는 무시한다 (ON CONFLICT DO NOTHING) — 리플레이나
    재시도가 원문을 덮어써서 기한만 늘리는 일이 없어야 한다.
    """
    if not policy.retains(trace.trace_id):
        return False
    stamp = now or datetime.now(UTC)
    await conn.execute(
        _INSERT,
        (
            trace.trace_id, trace.world_id, trace.actor_id, trace.tick, trace.purpose,
            trace.system_prompt, trace.user_prompt, trace.output, trace.model,
            stamp + policy.retention,
        ),
    )
    return True


async def read_trace(conn: AsyncConnection, trace_id: str) -> dict[str, Any] | None:
    """원문 조회 — 없으면 None.

    None은 '유실'이 아니라 '여기 없다'까지만 말한다. 남겼어야 했는지는
    actor.decision.made.trace_retained가 답한다 (모듈 docstring 2번).
    """
    cur = await conn.execute(
        "SELECT trace_id, world_id, actor_id, tick, purpose, system_prompt,"
        " user_prompt, output, model, created_at, expires_at"
        " FROM es.decision_traces WHERE trace_id = %s",
        (trace_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    keys = (
        "trace_id", "world_id", "actor_id", "tick", "purpose", "system_prompt",
        "user_prompt", "output", "model", "created_at", "expires_at",
    )
    return dict(zip(keys, row, strict=True))


async def purge_expired(
    conn: AsyncConnection, *, now: datetime | None = None, limit: int = 10_000
) -> int:
    """기한 지난 트레이스를 지운다 — 반환: 지운 건수.

    한 번에 다 지우지 않는다: 12GB/월 규모에서 무제한 DELETE는 긴 트랜잭션과
    잠금을 만든다. 배치가 반복 호출해 0이 될 때까지 비운다 (outbox purge 관례).
    """
    cur = await conn.execute(
        "DELETE FROM es.decision_traces WHERE trace_id IN ("
        " SELECT trace_id FROM es.decision_traces WHERE expires_at <= %s LIMIT %s)",
        (now or datetime.now(UTC), limit),
    )
    return cur.rowcount
