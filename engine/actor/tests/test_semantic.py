"""Semantic 기억 검증 — 결정적 임베딩(순수) + Qdrant 왕복(게이트) (ADR-007/008)."""

import os
import uuid

import pytest
from lf_actor.reflection import RETRACTED_CONFIDENCE, Belief, belief_point_key
from lf_actor.semantic import SemanticMemory, embed

QDRANT_URL = os.environ.get("LF_TEST_QDRANT_URL")


def test_embed_is_deterministic_and_normalized():
    a = embed("팀장이 내 기획안을 가로챘다")
    b = embed("팀장이 내 기획안을 가로챘다")
    assert a == b  # 리플레이 재현
    assert abs(sum(x * x for x in a) - 1.0) < 1e-4  # L2 정규화 (성분 6자리 라운딩 허용)


def test_embed_similarity_tracks_overlap():
    def cosine(x, y):
        return sum(a * b for a, b in zip(x, y, strict=True))

    base = embed("팀장이 내 기획안을 자기 이름으로 올렸다")
    related = embed("기획안을 팀장이 가로챈 일이 계속 떠오른다")
    unrelated = embed("오늘 점심은 국수를 먹었다")
    assert cosine(base, related) > cosine(base, unrelated)


pytestmark_integration = pytest.mark.skipif(
    QDRANT_URL is None, reason="LF_TEST_QDRANT_URL 미설정 — Qdrant 통합 스킵"
)


@pytestmark_integration
async def test_remember_and_recall_roundtrip():
    world = f"w_t{uuid.uuid4().hex[:8]}"  # 테스트 격리 — 세계당 컬렉션
    memory = SemanticMemory(QDRANT_URL)
    try:
        stored = await memory.remember(
            world, "a_aria_kim",
            event_id="01JZK7Q3W0000000000000000S",
            text="플레이어가 DM으로 기획안 일을 지지해줬다 — 고마움이 오래 남았다",
            importance=0.62,
            source_event_ids=["01JZK7Q3W0000000000000000G"],
        )
        assert stored
        await memory.remember(
            world, "a_minji_kim",
            event_id="01JZK7Q3W0000000000000000T",
            text="남의 기억 — 회상되면 안 된다 (규칙 5)",
            importance=0.9,
            source_event_ids=["01JZK7Q3W0000000000000000H"],
        )

        recalled = await memory.recall(world, "a_aria_kim", "기획안 지지 DM")
        assert any("지지해줬다" in text for text in recalled)
        assert all("남의 기억" not in text for text in recalled)  # 자기 기억만 (ADR-008 규칙 5)

        # 중요도 하한 — 임계보다 낮은 기억은 회상되지 않는다
        assert await memory.recall(world, "a_aria_kim", "기획안", min_importance=0.9) == []
    finally:
        await memory.close()


@pytestmark_integration
async def test_retracted_belief_falls_below_recall_floor():
    """신념 철회의 회상 배제 (ADR-008 신념 폐기): 철회가 같은 자리(point_key)를
    잔불 확신으로 덮어쓰면 회상 하한(0.3) 아래로 떨어진다 — 흐려진 믿음은
    더 이상 떠오르지 않는다. 별도 가중 로직 없이 계약의 합으로 동작한다."""
    world = f"w_t{uuid.uuid4().hex[:8]}"
    memory = SemanticMemory(QDRANT_URL)
    belief = Belief("관찰자는 힘이 되는 사람이다", "supporter", 0.5, "p_observer_0417", [])
    key = belief_point_key("a_aria_kim", belief)
    try:
        await memory.remember(
            world, "a_aria_kim",
            event_id="01JZK7Q3W0000000000000000A",
            text=belief.statement, importance=belief.confidence,
            source_event_ids=[], point_key=key,
        )
        recalled = await memory.recall(world, "a_aria_kim", "힘이 되는 사람")
        assert any("힘이 되는" in text for text in recalled)  # 확립된 신념은 떠오른다

        # 철회 — 같은 자리를 잔불 확신으로 덮어쓴다 (reflection retract_stale 계약)
        await memory.remember(
            world, "a_aria_kim",
            event_id="01JZK7Q3W0000000000000000B",
            text="힘이 되는 사람이라 믿었지만 — 지금은 그 확신이 흐려졌다",
            importance=RETRACTED_CONFIDENCE,
            source_event_ids=[], point_key=key,
        )
        assert await memory.recall(world, "a_aria_kim", "힘이 되는 사람") == []
    finally:
        await memory.close()