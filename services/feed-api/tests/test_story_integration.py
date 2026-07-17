"""es 사슬 왕복 통합 — append(SoT 쓰기)와 StoryReads(사슬 읽기)의 계약 공유 (PG 게이트).

정렬(global_seq)·무서사 제외·상한·이름 해석·'당신' 치환·started_by_you를
실제 PG 왕복으로 검증한다. es 직접 읽기의 예외 근거는 story.py 머리말.
"""

from contextlib import asynccontextmanager

from lf_eventstore import NewEvent, append
from lf_eventstore.migrate import migrate
from lf_feed_api.story import StoryReads
from lf_projector.pg_read import ReadStore

from .conftest import sample

WORLD = "w_main"
PLAYER = "p_observer_0417"
#: 사슬 뿌리 — 플레이어 DM 이벤트 id (correlation_id, 샘플과 동일)
CHAIN = "01JZK7Q3W0000000000000000G"


class OneConnPool:
    """테스트 대역 — 단일 연결을 psycopg_pool.connection() 모양으로 감싼다."""

    def __init__(self, conn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def connection(self):
        yield self._conn


async def _append_sample(pg, principal: str, name: str, *, head: int) -> None:
    """샘플 봉투를 원래 event_id·correlation_id 그대로 es에 적재한다."""
    env = sample(name)
    key = env.get("actor_id") or env["payload"].get("player_id") or "k"
    await append(
        pg, principal,
        [NewEvent(
            world_id=env["world_id"], stream=env["stream"], stream_key=key,
            type=env["type"], tick=env["tick"], actor_id=env.get("actor_id"),
            payload=env["payload"], event_id=env["event_id"],
            causation_id=env.get("causation_id"), correlation_id=env["correlation_id"],
        )],
        expected_head=head,
    )


async def seed_chain(pg) -> None:
    """샘플의 correlation G 사슬: 플레이어 DM → (tick 소음) → 감정 → 답장 → 관계.

    global_seq(적재 순서)가 곧 타임라인 순서다 — 샘플의 correlation_id를 그대로 쓴다.
    system.tick.started 샘플도 correlation G라 무서사 제외의 실측 재료가 된다.
    """
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    await _append_sample(pg, "engine.actor", "actor.identity.declared", head=0)  # 다른 사슬(W)
    await _append_sample(pg, "services.gateway", "player.dm.sent", head=0)       # 사슬 시작(G)
    await _append_sample(pg, "engine.tick", "system.tick.started", head=0)       # 소음 — 제외돼야
    await _append_sample(pg, "engine.actor", "actor.emotion.shifted", head=1)
    await _append_sample(pg, "engine.actor", "actor.message.sent", head=2)
    await _append_sample(pg, "engine.relationship", "relationship.state.changed", head=0)


async def seed_names(pg) -> None:
    """read.actors — 이름 해석의 원천 (pg fixture가 read 스키마를 비워둔 상태)."""
    store = ReadStore(pg)
    await store.ensure()
    await store.apply(sample("actor.identity.declared"))


async def test_chain_orders_excludes_noise_and_marks_authorship(pg):
    await seed_chain(pg)
    await seed_names(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, CHAIN, player_id=PLAYER, limit=50
    )
    # global_seq 순 + system.tick.* 제외 (무서사)
    assert [i["type"] for i in body["items"]] == [
        "player.dm.sent", "actor.emotion.shifted",
        "actor.message.sent", "relationship.state.changed",
    ]
    # 이름 해석: 요청자는 '당신', 액터는 read.actors의 이름
    assert [i["actor"] for i in body["items"]] == ["당신", "김아리", "김아리", "김아리"]
    # 요약: payload의 사람 문장 (dm text → emotion reason → …)
    assert body["items"][0]["summary"] == sample("player.dm.sent")["payload"]["text"]
    assert "인정받았다" in body["items"][1]["summary"]
    # 저자성 — 이 이야기의 원작자가 나 (plan/03 §단계 3→4)
    assert body["origin"] == body["items"][0]
    assert body["started_by_you"] is True


async def test_other_requesters_do_not_own_the_story(pg):
    await seed_chain(pg)
    stranger = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, CHAIN, player_id="p_stranger", limit=50
    )
    # 남의 개입은 익명 — 관찰자 프라이버시
    assert stranger["items"][0]["actor"] == "어느 관찰자"
    assert stranger["started_by_you"] is False

    anonymous = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, CHAIN, player_id=None, limit=50
    )
    assert anonymous["started_by_you"] is False


async def test_actor_origin_is_not_yours_and_unknown_type_labels(pg):
    await seed_chain(pg)
    # 사슬 W: 액터의 정체성 선언 하나 — origin이 player.*가 아니다
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, sample("actor.identity.declared")["correlation_id"],
        player_id=PLAYER, limit=50,
    )
    assert [i["type"] for i in body["items"]] == ["actor.identity.declared"]
    assert body["started_by_you"] is False
    # 요약 규칙에 없는 타입 — 타입 라벨 폴백 (숨기지 않는다)
    assert body["items"][0]["summary"] == "actor.identity.declared"


async def test_limit_caps_narrative_events(pg):
    await seed_chain(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(WORLD, CHAIN, player_id=PLAYER, limit=2)
    # 상한은 '이야기 항목' 기준이다 — 제외 타입(tick)이 자리를 차지하지 않는다
    assert [i["type"] for i in body["items"]] == ["player.dm.sent", "actor.emotion.shifted"]


async def test_missing_read_schema_degrades_to_anonymous_names(pg):
    await seed_chain(pg)  # read.actors 미시딩 — pg fixture가 read 스키마를 지운 상태
    body = await StoryReads(OneConnPool(pg)).timeline(WORLD, CHAIN, player_id=PLAYER, limit=50)
    # 이름 해석은 장식 — read 미구축이 사슬 조회를 죽이지 않는다 (익명 폴백)
    assert body["items"][1]["actor"] == "누군가"
    assert body["started_by_you"] is True


async def test_unknown_chain_is_empty_not_error(pg):
    await seed_chain(pg)
    body = await StoryReads(OneConnPool(pg)).timeline(
        WORLD, "01JZK7Q3W000000000000000ZZ"[:26], player_id=PLAYER, limit=50
    )
    assert body["items"] == []
    assert body["origin"] is None
    assert body["started_by_you"] is False
