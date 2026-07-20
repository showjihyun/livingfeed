"""세계 규모 계획 — 클램프·모델 하한·vRAM 권장·경고 (capacity.py)."""

from lf_actor.capacity import (
    MAX_ACTORS,
    MIN_ACTORS,
    MODEL_FLOOR_B_ABOVE_CAP,
    clamp_actor_count,
    format_plan,
    recommend_capacity,
)
from lf_actor.reload import PersonaReloader


def test_clamp_bounds():
    assert clamp_actor_count(5) == MIN_ACTORS  # 10 미만은 10으로
    assert clamp_actor_count(5000) == MAX_ACTORS  # 1000 초과는 1000으로
    assert clamp_actor_count(50) == 50


def test_small_world_allows_local_small_model():
    plan = recommend_capacity(15)
    assert plan.actor_count == 15
    assert plan.recommended_min_params_b < MODEL_FLOOR_B_ABOVE_CAP  # 20 미만은 소형 가능
    # 20명 미만이면 40B 하한 경고가 없다
    assert not any("40B 이상 모델만" in w for w in plan.warnings)


def test_twenty_plus_requires_40b_floor():
    plan = recommend_capacity(20)
    assert plan.recommended_min_params_b == MODEL_FLOOR_B_ABOVE_CAP
    assert any(str(MODEL_FLOOR_B_ABOVE_CAP) in w for w in plan.warnings)


def test_model_below_floor_warns_concretely():
    """20명↑에서 설정 모델이 40B 미만이면 구체 경고."""
    plan = recommend_capacity(50, model_params_b=8)
    assert any("⚠️" in w and "8B" in w for w in plan.warnings)
    # 모델이 하한 이상이면 그 경고는 없다
    ok = recommend_capacity(50, model_params_b=70)
    assert not any("⚠️" in w for w in ok.warnings)


def test_vram_grows_with_scale():
    small = recommend_capacity(10)
    big = recommend_capacity(500)
    assert big.vram_q4_gb > small.vram_q4_gb
    assert big.vram_fp16_gb >= big.vram_q4_gb  # fp16이 4비트보다 크다


def test_hosted_recommended_at_large_scale():
    assert recommend_capacity(500).hosted_recommended is True
    assert recommend_capacity(50).hosted_recommended is False


def test_clamp_warning_surfaced_in_plan():
    plan = recommend_capacity(5)
    assert plan.actor_count == MIN_ACTORS
    assert any("클램프" in w for w in plan.warnings)


def test_format_plan_has_key_lines():
    text = format_plan(recommend_capacity(100, model_params_b=8))
    assert "액터 100명" in text
    assert "권장 vRAM" in text
    assert "참고 GPU" in text
    assert "⚠️" in text  # 8B 모델 하한 위반 경고가 실린다


# ── 리로더 규모 상한 (LF_MAX_ACTORS) ──────────────────────────────────────────


def _seed(directory, n):
    for i in range(n):
        (directory / f"{i:03d}-x.yaml").write_text(
            f"id: a_x{i:03d}\nname: 인물{i}\n", encoding="utf-8"
        )


def test_reloader_caps_to_max_actors(tmp_path):
    _seed(tmp_path, 30)
    capped = PersonaReloader(tmp_path, max_actors=10).load()
    assert len(capped) == 10
    # 결정적 — 파일명 순 앞 10명
    assert [p.id for p in capped] == [f"a_x{i:03d}" for i in range(10)]


def test_reloader_no_cap_loads_all(tmp_path):
    _seed(tmp_path, 12)
    assert len(PersonaReloader(tmp_path).load()) == 12
    # 상한이 가용보다 크면 전원 로드
    assert len(PersonaReloader(tmp_path, max_actors=100).load()) == 12


# ── 초기 Hot 상한 (LF_HOT_START_ACTORS) — tick당 LLM 폭주 방지 ─────────────────


def _phases(n, hot_start_cap, hot_floor=0):
    from lf_actor.persona import Persona
    from lf_actor.phases import ActorPhases

    personas = [
        Persona(id=f"a_x{i:03d}", name=f"인물{i}", archetype="", identity_core="")
        for i in range(n)
    ]
    # ai·memory는 __init__에서 저장만 될 뿐 호출되지 않는다 (대역 주입 가능)
    return ActorPhases(
        personas, ai=object(), memory=object(),
        hot_start_cap=hot_start_cap, hot_floor=hot_floor,
    )


def test_hot_start_cap_bounds_initial_hot():
    from lf_tick.lod import Tier

    phases = _phases(30, hot_start_cap=8)
    tiers = [phases._lods[f"a_x{i:03d}"].tier for i in range(30)]
    assert sum(t is Tier.HOT for t in tiers) == 8  # 8명만 Hot
    assert sum(t is Tier.WARM for t in tiers) == 22  # 나머지는 Warm
    hot = sorted(a for a, lod in phases._lods.items() if lod.tier is Tier.HOT)
    assert hot == [f"a_x{i:03d}" for i in range(8)]  # 앞 8명(id순) — 결정적


def test_no_cap_starts_all_hot():
    from lf_tick.lod import Tier

    phases = _phases(30, hot_start_cap=None)  # 하위 호환 — 테스트 기본
    assert all(lod.tier is Tier.HOT for lod in phases._lods.values())


def test_cap_larger_than_world_starts_all_hot():
    from lf_tick.lod import Tier

    phases = _phases(5, hot_start_cap=8)  # 5 < 8 → 전원 Hot
    assert all(lod.tier is Tier.HOT for lod in phases._lods.values())


# ── 활기/유휴 모드 (LF_HOT_FLOOR) — 상시 Hot 바닥 ─────────────────────────────


def test_idle_mode_has_no_hot_floor():
    phases = _phases(30, hot_start_cap=6, hot_floor=0)  # 기본 유휴
    assert phases._hot_floor_ids == set()  # 아무도 상시 고정 안 됨


def test_lively_floor_pins_actors_hot_beyond_start_cap():
    """floor가 start_cap보다 커도 바닥 액터는 Hot으로 시작한다 (합집합)."""
    from lf_tick.lod import Tier

    phases = _phases(30, hot_start_cap=2, hot_floor=5)
    floor = {f"a_x{i:03d}" for i in range(5)}
    assert phases._hot_floor_ids == floor
    # 바닥 5명은 start_cap(2)을 넘어도 전부 Hot으로 시작
    assert all(phases._lods[a].tier is Tier.HOT for a in floor)
