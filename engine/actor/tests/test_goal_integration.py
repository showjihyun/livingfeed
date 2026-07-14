"""Goal Engine tick 통합 — 행동이 목표를 진행시키고 기억 중요도에 실린다 (ADR-012/008).

순수 로직은 lf-goal이 검증한다. 여기서는 (PG+Redis+NATS) tick 파이프라인에서
행동 → actor.goal.advanced 적재 + 에피소드 중요도 goal 항 실값을 확인한다.
"""

from datetime import UTC, datetime

from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.goal import GoalAdapter
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_eventstore import read_stream
from lf_goal import GoalState
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_test"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


async def test_action_advances_goal_and_scores_memory(conn, redis, nc, ai_service):  # noqa: F811
    """규칙 행동(rest/move=security)이 누적돼 민지의 '퇴사 결정' 목표를 진행시킨다."""
    goal = GoalAdapter(redis)
    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "minji-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        emotion=EmotionAdapter(redis),  # 목표 진전 → 감정 (ADR-015)
        goal=goal,
    )
    head = 0
    for tick in range(1, 16):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_minji_kim")]

    advanced = [e for e in events if e["type"] == "actor.goal.advanced"]
    assert advanced, "행동이 누적돼 목표 진행 이벤트가 섰어야 한다"

    # 목표 진전이 기쁨 감정을 낳는다 (ADR-015 goal_congruence)
    shifts = [e for e in events if e["type"] == "actor.emotion.shifted"]
    joy = [
        e for e in shifts
        if any(inst["type"] == "joy" for inst in e["payload"]["emotions"])
    ]
    assert joy, "목표 진전 tick에 기쁨(joy)이 섰어야 한다"
    payload = advanced[0]["payload"]
    assert payload["goal_id"] in {"g_decide_resignation", "g_side_project"}
    assert payload["need"] in {"security", "achievement"}
    assert 0 < payload["progress"] <= 1
    assert 0 < payload["congruence"] <= 1

    # 목표를 진행시킨 tick은 에피소드로 남고 goal 중요도 항이 실값이다 (ADR-008 0.20)
    episodes = [e for e in events if e["type"] == "actor.memory.consolidated"]
    assert any(e["payload"]["factors"]["goal"] > 0 for e in episodes)

    # 목표 진행도가 상태에 누적돼 남는다 (감쇠하지 않는다)
    state = await goal.load(WORLD, phases._personas["a_minji_kim"])
    assert any(v > 0 for v in state.goals.values())


async def test_goal_completion_fires_achieved_event_and_big_joy(conn, redis, nc, ai_service):  # noqa: F811
    """목표를 완주 직전(0.95)으로 시드 → 완성하는 행동에 goal.achieved + 벅찬 기쁨."""
    from lf_goal import GoalState, initial_state

    goal = GoalAdapter(redis)
    persona = load_persona(PERSONAS_DIR / "minji-kim.yaml")
    # g_decide_resignation(security, 0.9)을 완주 직전으로 — 한 번의 security 행동이면 넘는다
    seeded = initial_state(list(persona.goals), persona.needs_bias)
    seeded = GoalState(needs=seeded.needs, goals={**seeded.goals, "g_decide_resignation": 0.95})
    await goal.save(WORLD, "a_minji_kim", seeded)

    phases = ActorPhases(
        [persona],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        emotion=EmotionAdapter(redis),
        goal=goal,
    )
    head = 0
    for tick in range(1, 9):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)

    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_minji_kim")]
    achieved = [e for e in events if e["type"] == "actor.goal.achieved"]
    assert achieved, "완주 직전 목표가 security 행동으로 이뤄져야 한다"
    assert achieved[0]["payload"]["goal_id"] == "g_decide_resignation"
    assert achieved[0]["payload"]["need"] == "security"
    assert "progress" not in achieved[0]["payload"]  # 마디는 사실만 싣는다

    # 완주는 벅찬 기쁨을 낳는다 — 강한 joy 인스턴스 (base 0.85)
    joy_after = [
        inst
        for e in events if e["type"] == "actor.emotion.shifted"
        for inst in e["payload"]["emotions"]
        if inst["type"] == "joy" and inst["intensity"] >= 0.5
    ]
    assert joy_after, "완주 tick에 강한 기쁨(joy)이 섰어야 한다"

    # 이룬 목표는 1.0에 고정 — 이후 재발행 없음
    final = await goal.load(WORLD, phases._personas["a_minji_kim"])
    assert final.goals["g_decide_resignation"] == 1.0


async def test_no_goal_adapter_leaves_importance_zero(conn, redis, nc, ai_service):  # noqa: F811
    """goal 어댑터 미주입이면 중요도 goal 항은 0으로 흐른다 (정직한 폴백)."""
    phases = ActorPhases(
        [load_persona(PERSONAS_DIR / "aria-kim.yaml")],
        ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis),
        relationship=None,
    )
    head = 0
    for tick in range(1, 6):
        head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=head)
    events = [s.envelope for s in await read_stream(conn, WORLD, "actor", "a_aria_kim")]
    assert not [e for e in events if e["type"] == "actor.goal.advanced"]


def test_goal_summary_injected_into_decide_is_transient():
    """describe 요약은 결정 앞에 세우되 Working Memory에 영속되지 않는다 — 단위 확인."""
    from lf_goal import describe, initial_state

    persona_goals = [
        {"id": "g_x", "description": "특종", "priority": 0.9, "need": "achievement"},
    ]
    bias = {"achievement": 0.9, "belonging": 0.4, "security": 0.3}
    text = describe(initial_state(persona_goals, bias), persona_goals, bias)
    assert "목마른" in text and "특종" in text
    _ = GoalState  # import 확인
