"""Life Arc 저장 검증 — Director의 아크가 남고, 다음 계획이 덮어쓴다 (ADR-013).

Redis 필요 (없으면 skip — conftest 참고).
"""

from lf_actor.arc import Arc, ArcStore

WORLD = "w_test"
ARIA = "a_aria_kim"


async def test_arc_roundtrip_and_overwrite(redis):
    store = ArcStore(redis)
    assert await store.get(WORLD, ARIA) is None  # 아직 아크 없음 — 일상을 산다

    await store.set(WORLD, ARIA, "newcomer", "이 도시에서 자기 자리를 만들기 시작한다")
    assert await store.get(WORLD, ARIA) == Arc(
        stage="newcomer", intention="이 도시에서 자기 자리를 만들기 시작한다"
    )

    # 아크는 지속 상태 — 다음 계획이 통째로 덮어쓴다 (부분 갱신 없음)
    await store.set(WORLD, ARIA, "settling", "떠돌기를 멈추고 뿌리를 내린다")
    assert await store.get(WORLD, ARIA) == Arc(
        stage="settling", intention="떠돌기를 멈추고 뿌리를 내린다"
    )


async def test_arc_is_isolated_per_actor_and_world(redis):
    store = ArcStore(redis)
    await store.set(WORLD, ARIA, "prime", "정점에서 다음 이유를 찾는다")
    assert await store.get(WORLD, "a_junho_park") is None  # 남의 아크가 아니다
    assert await store.get("w_other", ARIA) is None  # 세계 경계도 지킨다
