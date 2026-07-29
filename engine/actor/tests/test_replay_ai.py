"""기록된 LLM 출력의 재생 — L0 리플레이 러너의 키스톤 (ADR-021 §4).

가장 중요한 단정: **기록이 없으면 거부한다.** 조용히 빈 결과를 돌려주면 엔진이
그것을 LLM 실패로 읽고 규칙 경로로 가서 세계가 갈라지고, 그 발산이 나중에
'검증 실패'로 오독된다 — 없는 사고를 만드는 것이다.
"""

from datetime import UTC, datetime

import pytest
from lf_actor.client import AiRuntimeClient
from lf_actor.context import WorldContext, build
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_persona
from lf_actor.phases import ActorPhases
from lf_actor.replay_ai import MissingRecordedOutput, ReplayAiClient, TracePlayback
from lf_eventstore import TracePolicy
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick

from .conftest import PERSONAS_DIR
from .test_phases import ai_service, nc  # noqa: F401 — 픽스처 재사용

WORLD = "w_replay"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))


def aria():
    return load_persona(PERSONAS_DIR / "aria-kim.yaml")


def bundle_for(purpose: str = "decide_action"):
    return build(
        aria(), [], WorldContext(world_id=WORLD, tick=0, world_time=CLOCK.world_time_at(0)),
        purpose=purpose, trace_id="t",
    )


async def test_missing_record_refuses_instead_of_falling_back(conn):
    """기록이 없으면 멈춘다 — 빈 결과는 세계를 갈라 놓는다."""
    client = ReplayAiClient(await TracePlayback.load(conn, WORLD))
    with pytest.raises(MissingRecordedOutput, match="재생할 기록이 없다"):
        await client.decide_action(
            bundle_for(), {}, tier="hot", actor_id=aria().id, tick=0
        )


async def test_recorded_run_replays_its_own_outputs(conn, redis, nc, ai_service):  # noqa: F811
    """연구 모드로 남긴 세계는 그대로 재생된다 — 모델을 다시 부르지 않는다."""
    phases = ActorPhases(
        [aria()], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), trace_policy=TracePolicy.research(),
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    playback = await TracePlayback.load(conn, WORLD)
    assert playback.remaining >= 1

    client = ReplayAiClient(playback)
    result = await client.decide_action(
        bundle_for(), {}, tier="hot", actor_id=aria().id, tick=0
    )
    # 그때 나온 답 그대로다 — 모델을 다시 부르지 않았다
    assert result.value is not None
    assert result.value["action_kind"]
    assert result.value["decision_trace"]["trace_id"]
    # 재생임이 기록에 남는다 — 원본의 모델 이름을 흉내내면 재생 세계가 원본인 척한다
    assert result.model == "replay:recorded"
    assert playback.remaining == 0  # 기록을 정확히 한 번씩 소진했다

    # 그리고 그 답은 실제로 세계에 남은 행동과 같다
    from lf_eventstore import read_stream

    stored = await read_stream(conn, WORLD, "actor", aria().id)
    [action] = [
        s.envelope for s in stored if s.envelope["type"] == "actor.action.performed"
    ]
    assert result.value["intent"] == action["payload"]["intent"]


async def test_sampled_out_traces_are_not_silently_replayable(conn, redis, nc, ai_service):  # noqa: F811
    """기본 모드(샘플링)의 기록으로는 세계를 다시 돌릴 수 없다 — 그것이 §5의 대가다."""
    phases = ActorPhases(
        [aria()], ai=AiRuntimeClient(nc, ai_service, timeout_s=5),
        memory=WorkingMemory(redis), trace_policy=TracePolicy(sample_rate=0.0),
    )
    await run_tick(conn, phases, CLOCK, WORLD, tick=0, head=0)

    playback = await TracePlayback.load(conn, WORLD)
    client = ReplayAiClient(playback)
    # 원문이 없는 '성공' 호출은 큐에 담기지 않는다 — 조용한 폴백 대신 거부가 된다.
    # (이 세계의 decide는 rule 프로바이더라 실패였고, 실패는 원문 없이도 재생된다)
    for _ in range(playback.remaining):
        await client.decide_action(bundle_for(), {}, tier="hot", actor_id=aria().id, tick=0)
    with pytest.raises(MissingRecordedOutput):
        await client.decide_action(bundle_for(), {}, tier="hot", actor_id=aria().id, tick=0)


async def test_playback_is_ordered_within_a_key():
    """같은 (액터, tick, purpose)가 여러 번이면 기록된 순서대로 소진한다."""
    from collections import deque

    from lf_actor.replay_ai import RecordedCall

    playback = TracePlayback({
        ("a_x", 3, "reply_to_player"): deque([
            RecordedCall("reply_to_player", "acted", '"첫 번째"'),
            RecordedCall("reply_to_player", "acted", '"두 번째"'),
        ])
    })
    assert playback.take("a_x", 3, "reply_to_player").output == '"첫 번째"'
    assert playback.take("a_x", 3, "reply_to_player").output == '"두 번째"'
    assert playback.remaining == 0
    with pytest.raises(MissingRecordedOutput):
        playback.take("a_x", 3, "reply_to_player")
