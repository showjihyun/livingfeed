"""정체성 선언 검증 — 페르소나 identity가 read 모델로 나가는 시작점 (ADR-012/003).

액터는 세계 등장 시 이름·소개·목표를 이벤트로 선언한다. 세계당 1회(재시작에도
중복 없음) — pg-projector가 이걸 read.actors로 접어 FE가 실제 이름을 읽는다.
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def _phases(redis, nc, ai_service):  # noqa: F811
    return ActorPhases(
        [load_persona(PERSONAS_DIR / "minji-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        identity_redis=redis,
    )


async def test_identity_declared_once_with_persona_fields(conn, redis, nc, ai_service):  # noqa: F811
    phases = _phases(redis, nc, ai_service)
    head = await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=0)
    await run_tick(conn, phases, CLOCK, WORLD, tick=2, head=head)  # 재확인 — 재발행 금지

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_minji_kim")]
    declared = [e for e in events if e["type"] == "actor.identity.declared"]
    assert len(declared) == 1  # 세계당 1회
    payload = declared[0]["payload"]
    assert payload["name"] == "김민지"
    assert payload["archetype"] == "quiet_builder"
    assert "프로덕트 디자이너" in payload["bio"]
    assert "\n" not in payload["bio"]  # 소개문은 한 줄로 정리된다
    goals = {g["description"] for g in payload["goals"]}
    assert "몰래 진행 중인 사이드 프로젝트를 완성한다" in goals


async def test_no_declaration_without_redis(conn, redis, nc, ai_service):  # noqa: F811
    # identity_redis 미주입이면 선언하지 않는다 (world는 no-op)
    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "minji-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=1, head=0)
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_minji_kim")]
    assert not [e for e in events if e["type"] == "actor.identity.declared"]
