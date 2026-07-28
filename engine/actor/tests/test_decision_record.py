"""결정 기록 E2E — ADR-009 §3이 약속하고 ADR-021 §2가 완결한 계약.

핵심은 L1이다: 리플레이 때 같은 입력으로 번들을 다시 조립하면 기록된 digest와
일치해야 한다. LLM 출력이 재현 불가능해도(§4 L3) **입력은 재현 가능하다**는 것이
연구용 관측성의 실질이며, 그 증명이 여기 있다.
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.context import DigestVerdict, WorldContext, build, verify_digest
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import TracePolicy, read_stream, read_trace
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_decision"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
ACTOR = "a_aria_kim"


def make_phases(nc, redis, env: str, *, policy: TracePolicy | None = None) -> ActorPhases:  # noqa: F811, E501
    return ActorPhases(
        [load_persona(PERSONAS_DIR / "aria-kim.yaml")],
        ai=AiRuntimeClient(nc, env, timeout_s=5),
        memory=WorkingMemory(redis),
        trace_policy=policy,
    )


async def decisions_in(conn) -> list[dict]:
    return [
        s.envelope
        for s in await read_stream(conn, WORLD, "actor", ACTOR)
        if s.envelope["type"] == "actor.decision.made"
    ]


async def test_decision_records_what_the_actor_knew(conn, redis, nc, ai_service):  # noqa: F811
    """결정마다 '무엇을 보고 그렇게 했는가'가 남는다 (ADR-009 §3의 완결)."""
    phases = make_phases(nc, redis, ai_service)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    [decision] = await decisions_in(conn)
    payload = decision["payload"]
    assert payload["purpose"] == "decide_action"
    assert payload["tier"] == "hot"
    assert payload["outcome"] in {"acted", "hesitated"}

    # 섹션 구조가 남는다 — 접히기 전의 모습 (ADR-009의 고정 순서 그대로)
    kinds = [s["kind"] for s in payload["sections"]]
    assert kinds[0] == "identity"
    assert kinds[-1] == "task_frame"
    assert "working" in kinds and "world" in kinds
    assert all(s["token_count"] > 0 for s in payload["sections"])

    # 출처는 derived다 — 이 이벤트는 조립기가 남긴 사실이지 LLM의 해석이 아니다
    assert decision["provenance"] == {
        "kind": "derived",
        "rule_id": "context.assemble:decide_action",
    }


async def test_recorded_digest_matches_a_rebuilt_bundle(conn, redis, nc, ai_service):  # noqa: F811
    """L1 — 같은 입력으로 재조립하면 기록된 지문과 일치한다 (ADR-021 §4).

    조립이 순수 함수라는 성질이 여기서 값이 된다: 결정 시점의 컨텍스트가
    증명되므로, LLM 출력을 재현하지 못해도 "그때 무엇을 알고 있었나"는 답할 수 있다.
    """
    phases = make_phases(nc, redis, ai_service)
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)
    [decision] = await decisions_in(conn)
    recorded = decision["payload"]["bundle_digest"]

    # 첫 tick의 액터는 작업 기억이 비어 있다 — 그 상태를 그대로 재조립한다
    rebuilt = build(
        load_persona(PERSONAS_DIR / "aria-kim.yaml"),
        [],
        WorldContext(world_id=WORLD, tick=0, world_time=CLOCK.world_time_at(0)),
        purpose="decide_action",
        trace_id="재조립엔 trace_id가 영향을 주지 않는다",
    )
    assert verify_digest(recorded, rebuilt.digest) is DigestVerdict.MATCH

    # 입력이 달라지면 불일치 — 대조가 실제로 무언가를 잡는다는 확인
    drifted = build(
        load_persona(PERSONAS_DIR / "aria-kim.yaml"),
        ["tick 0: 있지도 않았던 기억"],
        WorldContext(world_id=WORLD, tick=0, world_time=CLOCK.world_time_at(0)),
        purpose="decide_action",
        trace_id="t",
    )
    assert verify_digest(recorded, drifted.digest) is DigestVerdict.MISMATCH


async def test_default_mode_records_the_decision_but_not_the_prompt(
    conn, redis, nc, ai_service,  # noqa: F811
):
    """기본 모드는 원문을 남기지 않는다 — 그러나 결정 기록 자체는 언제나 남는다.

    이것이 §5의 요점이다: 트레이스는 이벤트의 6배라 샘플링하지만,
    actor.decision.made는 L1 보증의 근거라 샘플링 대상이 아니다.
    """
    phases = make_phases(nc, redis, ai_service, policy=TracePolicy(sample_rate=0.0))
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    [decision] = await decisions_in(conn)
    assert decision["payload"]["trace_retained"] is False
    assert await read_trace(conn, decision["payload"]["trace_id"]) is None


async def test_research_mode_keeps_the_prompt_and_says_so(conn, redis, nc, ai_service):  # noqa: F811
    """연구 모드는 원문을 남기고, 남겼다는 사실을 이벤트에 적는다.

    trace_retained가 없으면 빈 조회를 '유실'로 오해한다 — 없는 사고를 쫓게 된다.
    """
    phases = make_phases(nc, redis, ai_service, policy=TracePolicy.research())
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    [decision] = await decisions_in(conn)
    assert decision["payload"]["trace_retained"] is True

    trace = await read_trace(conn, decision["payload"]["trace_id"])
    assert trace is not None
    assert trace["purpose"] == "decide_action"
    assert trace["actor_id"] == ACTOR
    # 원문은 이벤트가 아니라 여기에 산다 — 수명이 다르기 때문이다 (ADR-021 §5)
    assert "당신은" in trace["system_prompt"]
    assert "## 작업 기억" in trace["user_prompt"]
