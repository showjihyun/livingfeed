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


def make_composer(**scoring_kwargs) -> FeedComposer:
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
    )


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
