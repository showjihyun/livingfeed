"""샤드 배정 검증 — 결정적 모듈로 해싱 (ADR-012 Phase 2 §일관성 규칙 4)."""

from lf_tick.shard import shard_of


def test_shard_of_is_deterministic_and_bounded():
    ids = [f"a_actor_{i}" for i in range(50)]
    first = [shard_of(a, 4) for a in ids]
    assert first == [shard_of(a, 4) for a in ids]  # 같은 입력 → 같은 샤드
    assert all(0 <= s < 4 for s in first)
    assert len(set(first)) > 1  # 한 샤드에 몰빵되지 않는다 (분산)


def test_solo_world_is_always_shard_zero():
    # num_shards=1(솔로)은 분할 자체가 없다 — 오늘의 배선과 완전 동형
    assert shard_of("a_anyone", 1) == 0
    assert shard_of("a_anyone", 0) == 0
