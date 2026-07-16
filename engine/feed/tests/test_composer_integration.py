"""FeedComposer PG 통합 — 승격 적재와 중복 거부 (멱등성, ADR-014/017).

PostgreSQL 필요 (없으면 skip — conftest 참고). JetStream 배선(소비→relay)은
dispatcher 통합 테스트와 E2E 스모크가 검증한다.
"""

import json
from pathlib import Path

import pytest
from lf_eventstore import ConcurrencyConflict, NewEvent, append, read_stream
from lf_feed.compose import derive_post_id
from lf_feed.composer import FeedComposer
from lf_feed.config import Config
from lf_feed.scoring import ScoringConfig

SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "actor.action.performed.001.json"
    ).read_text(encoding="utf-8")
)

ARC_SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "system.director.arc_planned.001.json"
    ).read_text(encoding="utf-8")
)


def make_composer(*, narrator=None, **scoring_kwargs) -> FeedComposer:
    cfg = Config(
        pg_dsn="unused",
        nats_url="unused",
        env="test",
        personas_dir=Path("agents/personas-없음"),
        scoring=ScoringConfig(**scoring_kwargs),
    )
    return FeedComposer(
        cfg,
        actor_names={
            "a_aria_kim": "김아리", "a_junho_park": "박준호", "a_minji_kim": "김민지",
        },
        narrator=narrator,
    )


class _StubNarrator:
    """canned 서사 본문을 돌려주는 스텁 — 라이브 모델 없이 narrate 경로를 탄다."""

    def __init__(self, body: str | None) -> None:
        self._body = body
        self.calls: list[dict] = []

    async def narrate(self, *, title, body, participants, world_id, tick):
        self.calls.append({"title": title, "body": body, "participants": participants})
        return self._body


async def test_compose_once_appends_feed_post(conn):
    composer = make_composer()
    post_id = await composer.compose_once(conn, SAMPLE)

    assert post_id == derive_post_id(SAMPLE["event_id"])
    stored = await read_stream(conn, SAMPLE["world_id"], "feed", post_id)
    assert len(stored) == 1
    envelope = stored[0].envelope
    assert envelope["type"] == "feed.post.published"
    assert envelope["causation_id"] == SAMPLE["event_id"]
    assert envelope["correlation_id"] == SAMPLE["correlation_id"]
    assert envelope["payload"]["visibility"] == "world"


async def test_compose_once_rejects_duplicate_promotion(conn):
    await make_composer().compose_once(conn, SAMPLE)

    # 크래시 후 재전달 시나리오 — 재시작한 composer(신선한 희소성 창)가 같은
    # 원본을 다시 받아도, 같은 post_id → 스트림 CAS가 중복 승격을 거부한다
    with pytest.raises(ConcurrencyConflict):
        await make_composer().compose_once(conn, SAMPLE)

    post_id = derive_post_id(SAMPLE["event_id"])
    assert len(await read_stream(conn, SAMPLE["world_id"], "feed", post_id)) == 1


async def test_compose_once_below_threshold_writes_nothing(conn):
    composer = make_composer(threshold=0.99)
    assert await composer.compose_once(conn, SAMPLE) is None
    post_id = derive_post_id(SAMPLE["event_id"])
    assert await read_stream(conn, SAMPLE["world_id"], "feed", post_id) == []


INCIDENT_SAMPLE = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "packages" / "schemas" / "samples" / "world.incident.occurred.001.json"
    ).read_text(encoding="utf-8")
)


async def test_high_drama_post_gets_llm_narration(conn):
    """LLM 내레이션 (ADR-018 narrate) — 고드라마 포스트의 본문이 서사로 다듬어진다.

    승격 판정은 이미 끝난 뒤라 실패해도 템플릿이 그대로 간다 (조용한 강등).
    """
    narrator = _StubNarrator("촛불 아래, 피하던 얼굴들이 같은 테이블에 마주 앉았다.")
    composer = make_composer(narrator=narrator)
    post_id = await composer.compose_once(conn, INCIDENT_SAMPLE)
    assert post_id is not None
    [stored] = await read_stream(conn, INCIDENT_SAMPLE["world_id"], "feed", post_id)
    assert stored.envelope["payload"]["body"] == narrator._body  # 서사 본문으로 교체
    assert stored.envelope["payload"]["narration_kind"] == "llm"
    assert narrator.calls and narrator.calls[0]["title"]  # 원문이 재료로 전달됐다


async def test_narration_failure_keeps_template_body(conn):
    # 내레이터 실패(None) — 템플릿 본문이 그대로 간다 (조용한 강등)
    narrator = _StubNarrator(None)
    composer = make_composer(narrator=narrator)
    post_id = await composer.compose_once(conn, INCIDENT_SAMPLE)
    [stored] = await read_stream(conn, INCIDENT_SAMPLE["world_id"], "feed", post_id)
    assert stored.envelope["payload"]["body"] == INCIDENT_SAMPLE["payload"]["description"]
    assert stored.envelope["payload"]["narration_kind"] == "template"


async def test_low_drama_post_skips_narrator(conn):
    # 임계(0.6) 미만 드라마 — 내레이터를 부르지도 않는다 (비용은 고드라마의 것)
    narrator = _StubNarrator("불릴 일 없는 서사")
    composer = make_composer(narrator=narrator)
    assert await composer.compose_once(conn, SAMPLE) is not None
    assert narrator.calls == []


def boost_envelope(target: str, *, boost: float, until_tick: int, tick: int) -> dict:
    event_id = ARC_SAMPLE["event_id"][:-2] + "B0"
    return {
        "event_id": event_id, "stream": "system",
        "type": "system.director.feed_boosted", "schema_version": 1,
        "world_id": SAMPLE["world_id"], "actor_id": None, "tick": tick,
        "occurred_at": "2026-07-12T08:32:00Z", "causation_id": None,
        "correlation_id": event_id,
        "payload": {"target_actor_id": target, "boost": boost, "until_tick": until_tick},
    }


def work_action(event_id: str, tick: int) -> dict:
    """무대상 work — drama 0.25, 기본 임계(0.35)에 살짝 못 미치는 경계 행동."""
    envelope = json.loads(json.dumps(SAMPLE))
    envelope["event_id"] = event_id
    envelope["correlation_id"] = event_id
    envelope["tick"] = tick
    envelope["payload"]["action_kind"] = "work"
    envelope["payload"]["target_actor_id"] = None
    envelope["payload"].pop("headline", None)
    return envelope


async def test_boost_lights_actor_actions_until_expiry(conn):
    """편집 조명 (boost_feed, ADR-013/014) — 조명이 켜지면 경계의 행동이 임계를
    넘고, 기간이 지나면 꺼진다. 조명 이벤트 자체는 포스트가 아니다 (제어 신호)."""
    actor = SAMPLE["actor_id"]
    base = SAMPLE["event_id"][:-2]

    # 조명 없는 경계 행동 — 임계 미달 (0.125 + 희소 0.2 = 0.325 < 0.35)
    plain = make_composer()
    assert await plain.compose_once(conn, work_action(base + "C1", tick=50)) is None

    # 조명이 켜진 composer — 같은 행동이 boost 항(0.6×0.1)으로 임계를 넘는다
    lit = make_composer()
    assert await lit.compose_once(
        conn, boost_envelope(actor, boost=0.6, until_tick=100, tick=40)
    ) is None  # 제어 신호 — 포스트가 아니다
    post_id = await lit.compose_once(conn, work_action(base + "C2", tick=50))
    assert post_id is not None
    [stored] = await read_stream(conn, SAMPLE["world_id"], "feed", post_id)
    assert stored.envelope["payload"]["worthiness"] >= 0.35

    # 조명은 영원하지 않다 — until_tick부터 꺼진다. 키는 (세계, 액터)다
    world = SAMPLE["world_id"]
    assert lit._active_boost({"world_id": world, "actor_id": actor, "tick": 99}) == 0.6
    assert lit._active_boost({"world_id": world, "actor_id": actor, "tick": 100}) == 0.0
    assert lit._active_boost(
        {"world_id": world, "actor_id": "a_junho_park", "tick": 50}
    ) == 0.0  # 남의 조명이 아니다
    assert lit._active_boost(
        {"world_id": "w_other", "actor_id": actor, "tick": 50}
    ) == 0.0  # 같은 id라도 다른 세계의 조명이 아니다 (composer는 전 세계를 소비한다)


def arc_envelope(event_id: str, stage: str) -> dict:
    envelope = json.loads(json.dumps(ARC_SAMPLE))
    envelope["event_id"] = event_id
    envelope["correlation_id"] = event_id
    envelope["payload"]["stage"] = stage
    return envelope


async def seed_arc(conn, envelope: dict, head: int) -> None:
    """아크 계획을 es arc 스트림에 심는다 — composer의 '이전 stage' 원천."""
    await append(
        conn, "engine.director",
        [
            NewEvent(
                world_id=envelope["world_id"], stream="system", stream_key="arc",
                type="system.director.arc_planned", tick=envelope["tick"],
                event_id=envelope["event_id"], payload=envelope["payload"],
            )
        ],
        expected_head=head,
    )


async def test_arc_transition_promotes_only_on_stage_change(conn):
    """장이 넘어갈 때만 서사다 — 첫 아크는 첫 장, 같은 stage 재계획은 조용 (plan/08)."""
    composer = make_composer()
    base = ARC_SAMPLE["event_id"][:-1]

    # 첫 아크 — 이야기의 첫 장이 열린다
    first = arc_envelope(base + "V", "settling")
    post1 = await composer.compose_once(conn, first)
    assert post1 is not None
    [stored] = await read_stream(conn, first["world_id"], "feed", post1)
    assert "첫 장이 열리다" in stored.envelope["payload"]["title"]
    assert stored.envelope["payload"]["participants"] == ["a_minji_kim"]
    await seed_arc(conn, first, head=0)

    # 장 전환 (settling → prime) — 넘어가는 순간이 승격된다
    second = arc_envelope(base + "X", "prime")
    post2 = await composer.compose_once(conn, second)
    assert post2 is not None
    [stored] = await read_stream(conn, second["world_id"], "feed", post2)
    assert "인생의 장이 넘어가다" in stored.envelope["payload"]["title"]
    body = stored.envelope["payload"]["body"]
    assert "정착·방황기" in body and "전성기·침체기" in body
    await seed_arc(conn, second, head=1)

    # 같은 장의 재계획 — 방향만 바뀌었다, 장은 안 넘어갔다 (조용)
    third = arc_envelope(base + "Z", "prime")
    assert await composer.compose_once(conn, third) is None
