"""FeedComposer PG 통합 — 승격 적재와 중복 거부 (멱등성, ADR-014/017).

PostgreSQL 필요 (없으면 skip — conftest 참고). JetStream 배선(소비→relay)은
dispatcher 통합 테스트와 E2E 스모크가 검증한다.
"""

import json
from pathlib import Path

import pytest
from lf_eventstore import ConcurrencyConflict, read_stream
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


def make_composer(**scoring_kwargs) -> FeedComposer:
    cfg = Config(
        pg_dsn="unused",
        nats_url="unused",
        env="test",
        personas_dir=Path("agents/personas-없음"),
        scoring=ScoringConfig(**scoring_kwargs),
    )
    return FeedComposer(cfg, actor_names={"a_aria_kim": "김아리", "a_junho_park": "박준호"})


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
