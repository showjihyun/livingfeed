"""L1 러너 — 결정 시점의 컨텍스트를 다시 조립해 대조한다 (ADR-021 §4).

가장 중요한 단정은 **거짓 사고를 만들지 않는다**는 것이다: 재조립 입력을 다
복원하지 못했으면 지문이 어긋나도 MISMATCH가 아니라 UNVERIFIABLE이어야 한다.
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_actor.replay_l1 import L1Verdict, verify_world
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_l1"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def aria():
    return load_persona(PERSONAS_DIR / "aria-kim.yaml")


def make_phases(nc, redis, env: str) -> ActorPhases:  # noqa: F811
    return ActorPhases(
        [aria()], ai=AiRuntimeClient(nc, env, timeout_s=5), memory=WorkingMemory(redis)
    )


async def test_opening_decision_is_fully_verified(conn, redis, nc, ai_service):  # noqa: F811
    """세계의 첫 결정은 완전히 재조립된다 — 작업 기억이 비었음이 증명되기 때문이다."""
    phases = make_phases(nc, redis, ai_service)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.total == 1
    assert report.verified == 1
    assert report.ok
    assert [c.verdict for c in report.checks] == [L1Verdict.MATCH]


async def test_later_decisions_are_unverifiable_not_failed(conn, redis, nc, ai_service):  # noqa: F811
    """작업 기억이 쌓인 뒤의 결정은 '검증 불가'다 — 어긋남으로 세면 없는 사고가 된다.

    이것이 이 모듈의 핵심 계약이다: 복원 못 한 입력으로 대조해 놓고 실패라
    부르면, 리포트가 거짓 회귀를 쏟아낸다.
    """
    phases = make_phases(nc, redis, ai_service)
    head = 0
    for tick in range(3):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.total == 3
    assert report.verified == 1  # 첫 결정만
    assert report.mismatched == []  # 어긋남은 하나도 없다
    assert len(report.unverifiable) == 2
    assert report.ok  # 검증 불가는 실패가 아니다

    blocked = report.summary()["blocked_by"]
    assert blocked == {"working": 2}  # 무엇이 커버리지를 막는지가 이름으로 남는다


async def test_missing_persona_blocks_instead_of_failing(conn, redis, nc, ai_service):  # noqa: F811
    """페르소나는 저작물이라 이벤트에서 복원되지 않는다 — 없으면 검증 불가다."""
    phases = make_phases(nc, redis, ai_service)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    report = await verify_world(conn, WORLD, [], world_time_at=CLOCK.world_time_at)
    assert report.mismatched == []
    assert report.unverifiable[0].unresolved == ("persona",)


async def test_tampered_digest_is_a_real_mismatch(conn, redis, nc, ai_service):  # noqa: F811
    """입력을 다 복원한 상태의 어긋남만 사고다 — 그때는 확실히 잡아야 한다."""
    phases = make_phases(nc, redis, ai_service)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    # 기록된 지문을 손댄다 (같은 조립기 버전을 유지해 '검증 불가'가 아니게)
    await conn.execute(
        "UPDATE es.events SET payload = jsonb_set(payload, '{bundle_digest}',"
        " to_jsonb('v1:sha256:' || repeat('0', 64)))"
        " WHERE world_id = %s AND type = 'actor.decision.made'",
        (WORLD,),
    )

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert not report.ok
    assert len(report.mismatched) == 1
    assert report.verified == 0


async def test_unknown_assembler_version_is_unverifiable(conn, redis, nc, ai_service):  # noqa: F811
    """조립기가 바뀌면 과거 지문은 전부 달라진다 — 그것을 실패로 읽으면 안 된다."""
    phases = make_phases(nc, redis, ai_service)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    await conn.execute(
        "UPDATE es.events SET payload = jsonb_set(payload, '{bundle_digest}',"
        " to_jsonb('v0:sha256:' || repeat('0', 64)))"
        " WHERE world_id = %s AND type = 'actor.decision.made'",
        (WORLD,),
    )

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.ok  # 실패가 아니다
    assert report.unverifiable[0].unresolved == ("assembler_version",)
