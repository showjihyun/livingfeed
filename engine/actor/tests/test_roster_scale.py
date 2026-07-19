"""로스터 규모 불변식 — 100 액터 + 유효한 커뮤니티 소속 (docs/plan/09 MVP).

시드가 줄거나 커뮤니티 참조가 깨지면 이 테스트가 잡는다. 세계의 실 데이터
(agents/personas, agents/communities.yaml)를 직접 읽는 계약 검증이다.
"""

from pathlib import Path

import yaml
from lf_actor.persona import load_personas

REPO = Path(__file__).resolve().parents[3]
PERSONAS_DIR = REPO / "agents" / "personas"
COMMUNITIES_FILE = REPO / "agents" / "communities.yaml"

BIG_FIVE = {"openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"}
NEEDS = {"achievement", "belonging", "security"}


def _community_ids() -> set[str]:
    docs = yaml.safe_load(COMMUNITIES_FILE.read_text(encoding="utf-8")) or []
    return {d["id"] for d in docs if isinstance(d, dict) and d.get("id")}


def test_world_has_at_least_100_active_actors():
    personas = load_personas(PERSONAS_DIR)
    assert len(personas) >= 100, f"활성 액터 {len(personas)}명 — MVP는 100명 규모"


def test_actor_ids_are_unique():
    personas = load_personas(PERSONAS_DIR)
    ids = [p.id for p in personas]
    assert len(ids) == len(set(ids)), "중복 액터 id"


def test_every_persona_has_complete_traits():
    for p in load_personas(PERSONAS_DIR):
        assert set(p.big_five) == BIG_FIVE, f"{p.id} big_five 키 불완전"
        assert set(p.needs_bias) == NEEDS, f"{p.id} needs_bias 키 불완전"
        assert p.id.startswith("a_") and p.name.strip()


def test_community_references_resolve():
    """모든 소속은 정의된 커뮤니티를 가리켜야 한다 (댕글링 참조 금지)."""
    defined = _community_ids()
    assert defined, "communities.yaml 이 비었다"
    for p in load_personas(PERSONAS_DIR):
        if p.community is not None:
            assert p.community in defined, f"{p.id} → 미정의 커뮤니티 {p.community}"


def test_all_communities_are_populated():
    """정의된 커뮤니티마다 최소 한 명은 소속한다 (빈 커뮤니티 = 죽은 탭)."""
    defined = _community_ids()
    used = {p.community for p in load_personas(PERSONAS_DIR) if p.community}
    assert defined <= used, f"소속 없는 커뮤니티: {sorted(defined - used)}"
