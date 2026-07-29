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


async def test_quiet_actor_verifies_across_ticks(conn, redis, nc, ai_service):  # noqa: F811
    """아무도 말 걸지 않은 인물은 여러 tick에 걸쳐 전부 검증된다.

    작업 기억의 자기 기록 부분은 로그에서 접힌다 (memo.py를 엔진과 공유한다).
    지각이 없었다면 그것이 작업 기억의 전부이므로 복원이 완결된다.
    """
    phases = make_phases(nc, redis, ai_service)
    head = 0
    for tick in range(3):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.total == 3
    assert report.verified == 3  # 첫 결정만이 아니라 전부
    assert report.mismatched == []
    assert report.unverifiable == []


async def test_perceived_actor_is_unverifiable_not_failed(conn, redis, nc, ai_service):  # noqa: F811
    """지각이 있었으면 '검증 불가'다 — 어긋남으로 세면 없는 사고가 된다.

    이것이 이 모듈의 핵심 계약이다: 배달된 봉투의 서술과 감정 줄은 배달 이력·감정
    상태에 달려 있어 로그만으로 복원되지 않는다. 복원 못 한 입력으로 대조해 놓고
    실패라 부르면 리포트가 거짓 회귀를 쏟아낸다.
    """
    from lf_actor.mailbox import Mailbox
    from lf_eventstore import new_ulid

    mailbox = Mailbox(redis)
    event_id = new_ulid()
    await mailbox.push(WORLD, aria().id, {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "provenance": {"kind": "authored", "author_id": "p_watcher"},
        "payload": {"player_id": "p_watcher", "target_actor_id": aria().id,
                    "text": "요즘 어때요?"},
    })
    phases = ActorPhases(
        [aria()], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), mailbox=mailbox,
    )
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=head)

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.mismatched == []  # 거짓 사고 없음 — 이것이 요점이다
    assert report.ok
    assert any("working" in c.unresolved for c in report.unverifiable)
    assert report.summary()["blocked_by"].get("working")


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


async def test_reconstruction_gap_is_never_reported_as_drift(conn, redis, nc, ai_service):  # noqa: F811
    """복원이 틀렸을 때 '어긋남'이 아니라 '검증 불가'로 떨어진다.

    러너의 가정(지각 봉투는 이 세계 로그에 있다)이 깨지는 순간은 반드시 온다 —
    여기서는 로그를 거치지 않고 메일박스로 직접 배달해 그 상황을 만든다. 그때
    작업 기억에는 우리가 못 보는 줄이 섞이고, 대조를 강행하면 거짓 회귀가 된다.
    섹션별 토큰 수가 그 구멍을 먼저 잡아야 한다.
    """
    from lf_actor.mailbox import Mailbox
    from lf_eventstore import new_ulid

    mailbox = Mailbox(redis)
    event_id = new_ulid()
    await mailbox.push(WORLD, aria().id, {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "provenance": {"kind": "authored", "author_id": "p_ghost"},
        "payload": {"player_id": "p_ghost", "target_actor_id": aria().id,
                    "text": "로그를 거치지 않고 닿은 말"},
    })
    phases = ActorPhases(
        [aria()], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), mailbox=mailbox,
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.mismatched == []  # 거짓 사고 없음 — 이 가드의 존재 이유
    assert report.ok
    # 작업 기억의 크기가 어긋나 그 섹션이 이름으로 지목된다
    assert "working" in report.summary()["blocked_by"]


async def test_perceived_dm_is_verified_through_emotion_fold(conn, redis, nc, ai_service):  # noqa: F811
    """플레이어 개입을 받은 인물도 검증된다 — 감정 한 줄을 shift 이벤트에서 되살린다.

    작업 기억의 지각 부분은 (a) 봉투의 서술 (b) 감정 한 줄이다. (a)는 순수 함수고,
    (b)는 `describe(최종 상태)`인데 shift payload에 mood와 top_emotions가 실려 있어
    되살릴 수 있다 — 그 지각이 실제로 감정을 흔들었을 때에 한해.
    """
    from lf_actor.emotion import EmotionAdapter
    from lf_actor.mailbox import Mailbox
    from lf_eventstore import NewEvent, Provenance, append, new_ulid

    event_id = new_ulid()
    dm = {
        "event_id": event_id, "stream": "player", "type": "player.dm.sent",
        "schema_version": 1, "world_id": WORLD, "actor_id": None, "tick": 0,
        "occurred_at": "2026-03-01T00:00:00Z", "causation_id": None,
        "correlation_id": event_id,
        "provenance": {"kind": "authored", "author_id": "p_watcher"},
        "payload": {"player_id": "p_watcher", "target_actor_id": aria().id,
                    "text": "기획안 얘기 봤어요. 응원해요."},
    }
    # 운영과 같은 경로: 개입은 로그에 남고, 라우터가 그것을 메일박스로 옮긴다
    await append(
        conn, "services.gateway",
        [NewEvent(
            world_id=WORLD, stream="player", stream_key="p_watcher",
            type="player.dm.sent", tick=0, event_id=event_id,
            provenance=Provenance.authored("p_watcher"), payload=dm["payload"],
        )],
        expected_head=0,
    )
    mailbox = Mailbox(redis)
    await mailbox.push(WORLD, aria().id, dm)

    phases = ActorPhases(
        [aria()], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), mailbox=mailbox, emotion=EmotionAdapter(redis),
    )
    # player 스트림과 tick 스트림은 별개다 — tick head는 여전히 0에서 시작한다
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=head)

    report = await verify_world(conn, WORLD, [aria()], world_time_at=CLOCK.world_time_at)
    assert report.mismatched == []
    # tick 1의 결정은 tick 0의 지각(서술+감정)을 작업 기억에 담고 있다 — 그것이 복원된다
    assert report.verified >= 1, report.summary()
