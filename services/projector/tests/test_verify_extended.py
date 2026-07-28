"""pg·redis 프로젝션 무결성 검사 검증 (ADR-003/014 후속).

pg: 원천 적재→프로젝션→무결, 이벤트 하나 빼먹은 시나리오→어긋남 감지.
redis: fold_followers 순수(관계 stand-in ∪ 명시 선언 − 철회) + 인덱스 대비.
"""

from lf_eventstore import NewEvent, Provenance, append
from lf_eventstore.migrate import migrate
from lf_projector.pg_read import ReadStore
from lf_projector.pg_verify import verify_pg
from lf_projector.timeline import TimelineStore
from lf_projector.timeline_verify import fold_followers, verify_timeline

from .conftest import sample

WORLD = "w_main"
PLAYER = "p_observer_0417"


def test_fold_followers_mirrors_write_rules():
    """기대 팔로워 fold — timeline.py 쓰기 규칙과 동형이어야 무결성 검사가 정직하다."""
    rel_keys = [f"a_aria_kim|{PLAYER}", f"{PLAYER}|a_aria_kim", "a_aria_kim|a_junho_park"]
    follows = [
        {"player_id": "p_fan", "target_actor_id": "a_junho_park", "following": True},
        {"player_id": PLAYER, "target_actor_id": "a_aria_kim", "following": False},  # 철회가 이긴다
        {"player_id": "p_flip", "target_actor_id": "a_aria_kim", "following": False},
        {"player_id": "p_flip", "target_actor_id": "a_aria_kim", "following": True},  # 마지막 선언
    ]
    expected = fold_followers(rel_keys, follows)
    assert expected == {"a_aria_kim": {"p_flip"}, "a_junho_park": {"p_fan"}}
    assert fold_followers([], []) == {}


async def test_verify_pg_detects_missing_projection(pg):
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    store = ReadStore(pg)
    await store.ensure()

    # 원천 적재 (정체성 + 에피소드) — es가 진실이다
    for name, stream, key, principal in (
        ("actor.identity.declared", "actor", "a_aria_kim", "engine.actor"),
        ("actor.memory.consolidated", "actor", "a_aria_kim", "engine.actor"),
    ):
        envelope = sample(name)
        await append(
            pg, principal,
            [NewEvent(
                world_id=WORLD, stream=stream, stream_key=key, type=name,
                tick=envelope["tick"], actor_id=envelope["actor_id"],
                payload=envelope["payload"],
                provenance=Provenance.from_json(envelope["provenance"]),
            )],
            expected_head=0 if name == "actor.identity.declared" else 1,
        )
        stored = sample(name)  # 프로젝션은 원천과 같은 내용을 반영한다
        await store.apply(stored)

    report = await verify_pg(pg, world_id=WORLD)
    # 주의: 프로젝션은 샘플 event_id를 썼지만 개수·키 비교라 무결로 본다
    assert report["worlds"][WORLD]["tables"]["actors"]["ok"]
    assert report["worlds"][WORLD]["tables"]["actor_episodes"]["ok"]

    # 원천에만 있는 아크 — 프로젝션 누락을 감지한다
    arc = sample("system.director.arc_planned")
    await append(
        pg, "engine.director",
        [NewEvent(
            world_id=WORLD, stream="system", stream_key="arc",
            type="system.director.arc_planned", tick=arc["tick"], payload=arc["payload"],
            provenance=Provenance.from_json(arc["provenance"]),
        )],
        expected_head=0,
    )
    report = await verify_pg(pg, world_id=WORLD)
    assert not report["ok"]
    arcs = report["worlds"][WORLD]["tables"]["actor_arcs"]
    assert arcs["missing"] == ["a_minji_kim"]
    assert not report["worlds"][WORLD]["tables"]["actor_arc_history"]["ok"]


async def test_verify_timeline_detects_index_drift(pg, redis):
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)

    # 원천: 명시 팔로우 선언 하나
    follow = sample("player.follow.changed")
    await append(
        pg, "services.gateway",
        [NewEvent(
            world_id=WORLD, stream="player", stream_key=follow["payload"]["player_id"],
            type="player.follow.changed", tick=follow["tick"], payload=follow["payload"],
            provenance=Provenance.from_json(follow["provenance"]),
        )],
        expected_head=0,
    )

    # 인덱스가 비어 있다 — 어긋남
    report = await verify_timeline(pg, redis, world_id=WORLD)
    assert not report["ok"]
    assert "a_minji_kim" in report["worlds"][WORLD]["mismatched"]

    # 프로젝션 반영 — 무결
    await TimelineStore(redis).set_follow(
        WORLD, follow["payload"]["target_actor_id"],
        follow["payload"]["player_id"], True,
    )
    assert (await verify_timeline(pg, redis, world_id=WORLD))["ok"]


# ── 고아 프로젝션 — 원천에 없는 세계는 열거에서 빠지면 영영 안 보인다 ──


async def test_verify_pg_sees_orphan_world_projection_only(pg):
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    store = ReadStore(pg)
    await store.ensure()

    identity = sample("actor.identity.declared")
    await store.apply(identity)  # es에는 아무것도 없다 — 스모크 잔여물 시나리오

    report = await verify_pg(pg)  # 자동 열거
    world = report["worlds"][identity["world_id"]]
    assert not report["ok"]
    assert world["tables"]["actors"]["extra"] == [identity["actor_id"]]


async def test_verify_timeline_sees_orphan_world_index_only(pg, redis):
    await pg.execute("DROP SCHEMA IF EXISTS es CASCADE")
    await migrate(pg)
    await TimelineStore(redis).set_follow("w_ghost", "a_aria_kim", "p_ghost", True)

    report = await verify_timeline(pg, redis)  # 자동 열거 — es는 비어 있다
    assert not report["ok"]
    assert "a_aria_kim" in report["worlds"]["w_ghost"]["mismatched"]
