"""페르소나 스튜디오 백엔드 — 로더 확장·핫 리로드 (engine/actor 몫).

사람이 빚은 인물이 파일(SoT, ADR-001/012)에 저장되고, tick 워커가 재시작
없이 세계에 등장시킨다. 전부 tmp_path 페르소나 디렉터리 — 실제
agents/personas는 건드리지 않는다.
"""

from datetime import UTC, datetime
from pathlib import Path

from lf_actor.persona import load_persona, load_personas
from lf_actor.phases import ActorPhases
from lf_eventstore import read_stream
from lf_tick.lod import ActorLod, Tier
from lf_tick.pipeline import TickContext

WORLD = "w_test"
WORLD_TIME = datetime(2026, 3, 1, 12, tzinfo=UTC)


def ctx_at(tick: int, conn=None) -> TickContext:
    return TickContext(world_id=WORLD, tick=tick, world_time=WORLD_TIME, conn=conn)

MINIMAL = """\
id: {id}
name: {name}
archetype: test_subject
lifestyle: flexible
big_five:
  openness: 0.5
  conscientiousness: 0.5
  extraversion: 0.5
  agreeableness: 0.5
  neuroticism: 0.5
identity_core: 테스트 인물.
needs_bias:
  achievement: 0.5
  belonging: 0.5
  security: 0.5
goals: []
secrets: []
"""


def write_persona(
    directory: Path, filename: str, *, id: str, name: str = "테스트", extra: str = ""
) -> Path:
    path = directory / filename
    path.write_text(MINIMAL.format(id=id, name=name) + extra, encoding="utf-8")
    return path


# ── 구현 ① 로더 확장: active(휴면)·created_by(저자성) ─────────────────────────


def test_load_persona_reads_active_and_created_by(tmp_path):
    path = write_persona(
        tmp_path, "one.yaml", id="a_one", extra="active: false\ncreated_by: p_maker\n"
    )
    p = load_persona(path)
    assert p.active is False
    assert p.created_by == "p_maker"


def test_load_persona_defaults_are_backward_compatible(tmp_path):
    # 기존 yaml(active/created_by 없음)은 깨어 있는 시스템 태생으로 읽힌다
    p = load_persona(write_persona(tmp_path, "one.yaml", id="a_one"))
    assert p.active is True
    assert p.created_by is None


def test_load_personas_skips_dormant(tmp_path):
    # 휴면(active=false)은 세계에 실리지 않는다 — 파일·역사·관계는 남는다
    write_persona(tmp_path, "a.yaml", id="a_awake")
    write_persona(tmp_path, "b.yaml", id="a_dormant", extra="active: false\n")
    assert [p.id for p in load_personas(tmp_path)] == ["a_awake"]


# ── 정체성 선언의 저자성 — created_by가 데뷔 표식의 원천이 된다 ──────────────


async def test_identity_declaration_carries_created_by(tmp_path, conn, redis):
    # 스튜디오 태생 인물의 선언에는 빚은 플레이어가 실린다 ('당신이 빚은 인물')
    write_persona(tmp_path, "made.yaml", id="a_made", extra="created_by: p_maker\n")
    write_persona(tmp_path, "born.yaml", id="a_born")  # 시스템 태생 — null
    phases = ActorPhases(
        load_personas(tmp_path), ai=None, memory=None, identity_redis=redis
    )
    ctx = TickContext(world_id=WORLD, tick=3, world_time=WORLD_TIME, conn=conn)
    await phases.world(ctx)

    for actor_id, expected in (("a_made", "p_maker"), ("a_born", None)):
        [event] = await read_stream(conn, WORLD, "actor", actor_id)
        envelope = event.envelope
        assert envelope["type"] == "actor.identity.declared"
        assert envelope["payload"]["created_by"] == expected


# ── 구현 ② refresh_personas — 리로드가 세계의 명부를 tick 경계에서 바꾼다 ────


def personas_of(tmp_path: Path, *specs: tuple[str, str]) -> list:
    for filename, actor_id in specs:
        write_persona(tmp_path, filename, id=actor_id)
    return load_personas(tmp_path)


async def test_refresh_adds_new_actor_as_hot_debut(tmp_path):
    # 새 액터는 현재 tick 기준 Hot — 등장 직후 첫 decide가 바로 나온다 (데뷔의 손맛)
    phases = ActorPhases(personas_of(tmp_path, ("a.yaml", "a_one")), ai=None, memory=None)
    assert await phases.schedule(ctx_at(5)) == {"hot": 1, "warm": 0, "cold": 0}

    added, removed = phases.refresh_personas(
        personas_of(tmp_path, ("b.yaml", "a_two")), tick=5
    )
    assert (added, removed) == (["a_two"], [])
    assert phases._lods["a_two"] == ActorLod(tier=Tier.HOT, last_interest_tick=5)
    assert await phases.schedule(ctx_at(5)) == {"hot": 2, "warm": 0, "cold": 0}


async def test_refresh_removes_deactivated_actor_and_purges_state(tmp_path):
    phases = ActorPhases(
        personas_of(tmp_path, ("a.yaml", "a_one"), ("b.yaml", "a_two")), ai=None, memory=None
    )
    # 진행 중 자료구조에 남은 흔적 — 결손 키에 관대해야 한다 (리로드 요구)
    phases._seen_posts["a_two"] = [{"event_id": "01X", "payload": {}}]
    phases._inbox["a_two"] = [{"type": "player.dm.sent"}]

    (tmp_path / "b.yaml").unlink()
    added, removed = phases.refresh_personas(load_personas(tmp_path), tick=9)
    assert (added, removed) == ([], ["a_two"])
    assert await phases.schedule(ctx_at(9)) == {"hot": 1, "warm": 0, "cold": 0}
    assert "a_two" not in phases._lods
    assert "a_two" not in phases._seen_posts
    assert "a_two" not in phases._inbox


async def test_refresh_swaps_changed_persona_object(tmp_path):
    # 성격 등 필드 변경 → Persona 객체 교체 (다음 decide부터 새 결)
    phases = ActorPhases(personas_of(tmp_path, ("a.yaml", "a_one")), ai=None, memory=None)
    write_persona(tmp_path, "a.yaml", id="a_one", name="바뀐이름")
    added, removed = phases.refresh_personas(load_personas(tmp_path), tick=2)
    assert (added, removed) == ([], [])
    assert phases._personas["a_one"].name == "바뀐이름"


async def test_refresh_rejects_empty_roster(tmp_path):
    import pytest

    phases = ActorPhases(personas_of(tmp_path, ("a.yaml", "a_one")), ai=None, memory=None)
    with pytest.raises(ValueError):
        phases.refresh_personas([], tick=1)


async def test_readded_actor_does_not_redeclare_identity(tmp_path, conn, redis):
    # 휴면 후 재기상 — 정체성 선언은 세계당 1회 원칙 유지 (Redis SETNX가 권위)
    write_persona(tmp_path, "a.yaml", id="a_one")
    phases = ActorPhases(load_personas(tmp_path), ai=None, memory=None, identity_redis=redis)
    await phases.world(ctx_at(1, conn))

    write_persona(tmp_path, "a.yaml", id="a_one", extra="active: false\n")
    write_persona(tmp_path, "b.yaml", id="a_two")
    phases.refresh_personas(load_personas(tmp_path), tick=2)
    write_persona(tmp_path, "a.yaml", id="a_one")  # 다시 깨어난다
    phases.refresh_personas(load_personas(tmp_path), tick=3)
    await phases.world(ctx_at(3, conn))

    declared = [
        e.envelope
        for e in await read_stream(conn, WORLD, "actor", "a_one")
        if e.envelope["type"] == "actor.identity.declared"
    ]
    assert len(declared) == 1  # 재선언 없음
    # 새로 등장한 액터는 선언된다
    [event] = await read_stream(conn, WORLD, "actor", "a_two")
    assert event.envelope["type"] == "actor.identity.declared"


# ── 구현 ② 지문 감지 — 파일 목록 + max mtime, tick 사이에만 리로드 ───────────


def test_fingerprint_changes_on_add_remove_and_edit(tmp_path):
    import os

    from lf_actor.reload import scan_fingerprint

    write_persona(tmp_path, "a.yaml", id="a_one")
    base = scan_fingerprint(tmp_path)
    assert scan_fingerprint(tmp_path) == base  # 변화 없으면 같다 (순수)

    added = write_persona(tmp_path, "b.yaml", id="a_two")
    with_two = scan_fingerprint(tmp_path)
    assert with_two != base  # 파일 추가

    os.utime(added, ns=(0, added.stat().st_mtime_ns + 10_000_000_000))
    assert scan_fingerprint(tmp_path) != with_two  # 내용 수정(mtime 전진)

    added.unlink()
    assert scan_fingerprint(tmp_path) != with_two  # 파일 제거


def test_reloader_polls_only_on_change(tmp_path):
    import os

    from lf_actor.reload import PersonaReloader

    write_persona(tmp_path, "a.yaml", id="a_one")
    reloader = PersonaReloader(tmp_path)
    assert [p.id for p in reloader.load()] == ["a_one"]
    assert reloader.poll() is None  # 변화 없음 — 리로드 안 한다

    path = write_persona(tmp_path, "b.yaml", id="a_two")
    os.utime(path, ns=(0, path.stat().st_mtime_ns + 10_000_000_000))
    reloaded = reloader.poll()
    assert reloaded is not None and [p.id for p in reloaded] == ["a_one", "a_two"]
    assert reloader.poll() is None  # 반영 후엔 다시 조용하다


async def test_reloading_phases_refreshes_at_tick_boundary(tmp_path):
    # schedule(tick의 첫 관문) 직전에만 리로드 — tick 중간 변경 금지
    from lf_actor.reload import PersonaReloader, ReloadingPhases

    write_persona(tmp_path, "a.yaml", id="a_one")
    reloader = PersonaReloader(tmp_path)
    phases = ActorPhases(reloader.load(), ai=None, memory=None)
    rosters: list[list[str]] = []
    wrapped = ReloadingPhases(
        phases, reloader, on_reload=lambda ps: rosters.append([p.id for p in ps])
    )
    assert await wrapped.schedule(ctx_at(0)) == {"hot": 1, "warm": 0, "cold": 0}

    path = write_persona(tmp_path, "b.yaml", id="a_two")
    import os

    os.utime(path, ns=(0, path.stat().st_mtime_ns + 10_000_000_000))
    assert await wrapped.schedule(ctx_at(1)) == {"hot": 2, "warm": 0, "cold": 0}
    assert rosters == [["a_one", "a_two"]]


async def test_reloading_phases_survives_broken_yaml(tmp_path):
    # 편집 중 깨진 yaml — tick은 멈추지 않고 기존 명부가 유지된다
    from lf_actor.reload import PersonaReloader, ReloadingPhases

    write_persona(tmp_path, "a.yaml", id="a_one")
    reloader = PersonaReloader(tmp_path)
    phases = ActorPhases(reloader.load(), ai=None, memory=None)
    wrapped = ReloadingPhases(phases, reloader)

    broken = tmp_path / "b.yaml"
    broken.write_text("id: [broken", encoding="utf-8")
    assert await wrapped.schedule(ctx_at(1)) == {"hot": 1, "warm": 0, "cold": 0}

    # 저장을 마치면(mtime 전진) 다음 tick 경계에서 반영된다
    import os

    write_persona(tmp_path, "b.yaml", id="a_two")
    os.utime(broken, ns=(0, broken.stat().st_mtime_ns + 10_000_000_000))
    assert await wrapped.schedule(ctx_at(2)) == {"hot": 2, "warm": 0, "cold": 0}


async def test_feed_fanout_roster_follows_reload():
    # 리로드로 등장한 액터도 피드 순환의 독자가 된다 (main.py on_reload 배선)
    from lf_actor.social import FeedFanout

    class NoEdges:
        async def load(self, world_id, a, b):  # noqa: ANN001, ANN202
            return None

    fanout = FeedFanout(NoEdges(), ["a_one"], limit=3)
    post = {
        "world_id": WORLD, "event_id": "01POST", "actor_id": "a_one",
        "payload": {"visibility": "world"},
    }
    assert await fanout.targets(post) == []  # 혼자였다 — 독자가 없다

    fanout.set_roster(["a_one", "a_two"])
    assert await fanout.targets(post) == ["a_two"]  # 새 이웃이 글을 본다
