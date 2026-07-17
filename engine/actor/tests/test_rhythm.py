"""SNS 포스팅 리듬 — 생활 패턴(lifestyle)별 비정기 포스팅 모먼트 검증.

페르소나는 하루 중 흩어진 시각에 근황을 올리고 싶어진다: 주말·휴일엔 더 자주,
바쁜 생활 패턴은 평일 일과 시간에 억제된다. 전부 결정적(crc32 분산)이어야
리플레이가 재현된다. 수치는 lf_actor/params.yaml이 원천이다 (하드코딩 금지).
"""

import copy
from datetime import UTC, date, datetime, timedelta

from lf_actor.memory import WorkingMemory
from lf_actor.persona import Persona, load_persona
from lf_actor.phases import ActorPhases
from lf_actor.rhythm import default_params, posting_moment
from lf_eventstore import read_stream
from lf_tick.clock import TickClock
from lf_tick.engine import run_tick
from lf_tick.lod import ActorLod, Tier, is_due

#: 세계 하루 = 360 tick (1 tick = 세계 240초, ADR-011 시간 모델)
TICKS_PER_DAY = 360
WORLD_SECONDS_PER_TICK = 240


def day_moments(actor_id: str, lifestyle: str, day: datetime, params) -> list[int]:
    """그 날(자정 기준)의 포스팅 모먼트 slot(하루 내 tick 번호) 목록."""
    return [
        t
        for t in range(TICKS_PER_DAY)
        if posting_moment(
            actor_id, lifestyle, day + timedelta(seconds=t * WORLD_SECONDS_PER_TICK), params
        )
    ]


def test_persona_lifestyle_loads_and_defaults(tmp_path):
    """lifestyle은 YAML에서 읽고, 없거나 비면 flexible이 기본이다 (전방 호환)."""
    explicit = tmp_path / "office.yaml"
    explicit.write_text(
        "id: a_probe_office\nname: 사무직\nlifestyle: office_worker\n", encoding="utf-8"
    )
    assert load_persona(explicit).lifestyle == "office_worker"

    missing = tmp_path / "missing.yaml"
    missing.write_text("id: a_probe_free\nname: 자유인\n", encoding="utf-8")
    assert load_persona(missing).lifestyle == "flexible"

    empty = tmp_path / "empty.yaml"
    empty.write_text("id: a_probe_null\nname: 무표기\nlifestyle:\n", encoding="utf-8")
    assert load_persona(empty).lifestyle == "flexible"


def test_posting_moment_is_deterministic():
    """같은 (액터, 생활 패턴, 세계 시각, 파라미터) → 항상 같은 답 — 리플레이 조건."""
    params = default_params()
    monday = datetime(2026, 3, 2, tzinfo=UTC)
    first = day_moments("a_probe_office", "office_worker", monday, params)
    second = day_moments("a_probe_office", "office_worker", monday, params)
    assert first == second
    assert first  # 하루에 모먼트가 실제로 존재한다


def test_office_worker_weekday_suppressed_during_work_hours():
    """바쁜 직장인은 평일 09~18(일과 창) 전 구간에서 모먼트가 없다."""
    params = default_params()
    monday = datetime(2026, 3, 2, tzinfo=UTC)  # 평일
    busy = params["rhythm"]["lifestyles"]["office_worker"]["busy"]
    target = params["rhythm"]["lifestyles"]["office_worker"]["daily_posts"]["weekday"]

    moments = day_moments("a_probe_office", "office_worker", monday, params)
    hours = [slot // (3600 // WORLD_SECONDS_PER_TICK) for slot in moments]
    assert all(not (busy["start"] <= h < busy["end"]) for h in hours)
    assert abs(len(moments) - target) <= 1  # 하루 전체 모먼트 수 ≈ 평일 목표


def in_hours(slots: list[int], start: int, end: int) -> bool:
    return any(start <= slot // (3600 // WORLD_SECONDS_PER_TICK) < end for slot in slots)


def test_office_worker_weekend_amplifies_and_frees_work_hours():
    """주말엔 하루 목표가 늘고, 일과 창(09~18)도 풀린다 — 모먼트가 들어갈 수 있다."""
    params = default_params()
    posts = params["rhythm"]["lifestyles"]["office_worker"]["daily_posts"]
    assert posts["weekend"] > posts["weekday"]  # 파라미터 계약: 주말 증폭

    sunday = datetime(2026, 3, 1, tzinfo=UTC)  # genesis 일요일
    moments = day_moments("a_probe_office", "office_worker", sunday, params)
    assert abs(len(moments) - posts["weekend"]) <= 1

    # 창이 풀렸다 — 여러 액터를 보면 09~18에 떨어진 모먼트가 존재한다 (결정적 관측)
    probes = [f"a_probe_{i}" for i in range(8)]
    assert any(
        in_hours(day_moments(p, "office_worker", sunday, params), 9, 18) for p in probes
    )


def test_flexible_posts_even_during_business_hours_on_weekdays():
    """flexible은 일과 창이 없다 — 평일 09~18에도 모먼트가 존재할 수 있다."""
    params = default_params()
    monday = datetime(2026, 3, 2, tzinfo=UTC)
    probes = [f"a_probe_{i}" for i in range(8)]
    assert any(
        in_hours(day_moments(p, "flexible", monday, params), 9, 18) for p in probes
    )


def test_night_worker_weekday_window_crosses_midnight():
    """night_worker의 평일 창 22~07은 자정을 넘는다 — 양쪽 끝 모두 억제된다."""
    params = default_params()
    monday = datetime(2026, 3, 2, tzinfo=UTC)
    probes = [f"a_probe_{i}" for i in range(8)]
    for probe in probes:
        moments = day_moments(probe, "night_worker", monday, params)
        assert moments  # 억제 창 밖(07~22)에서는 산다
        hours = [slot // (3600 // WORLD_SECONDS_PER_TICK) for slot in moments]
        assert all(7 <= h < 22 for h in hours)


def test_holiday_weekday_behaves_like_weekend():
    """holidays에 든 평일은 주말처럼 — 창이 풀리고 목표가 주말 수로 늘어난다.

    yaml은 따옴표 없는 ISO 날짜를 date 객체로 파싱하므로 두 표기 모두 받는다.
    """
    for holiday_entry in ("2026-03-02", date(2026, 3, 2)):
        params = copy.deepcopy(default_params())
        params["rhythm"]["holidays"] = [holiday_entry]  # 월요일을 휴일로
        holiday = datetime(2026, 3, 2, tzinfo=UTC)
        posts = params["rhythm"]["lifestyles"]["office_worker"]["daily_posts"]

        moments = day_moments("a_probe_office", "office_worker", holiday, params)
        assert abs(len(moments) - posts["weekend"]) <= 1  # 주말 목표
        probes = [f"a_probe_{i}" for i in range(8)]
        assert any(  # 일과 창(09~18)이 풀렸다
            in_hours(day_moments(p, "office_worker", holiday, params), 9, 18) for p in probes
        )


def test_unknown_lifestyle_falls_back_to_flexible():
    """닫힌 어휘 밖의 lifestyle은 flexible과 동일 동작 — 전방 호환."""
    params = default_params()
    for day in (datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 2, tzinfo=UTC)):
        assert day_moments("a_probe_new", "astronaut", day, params) == day_moments(
            "a_probe_new", "flexible", day, params
        )


def test_moments_scatter_across_dates():
    """날짜가 다르면 모먼트 시각이 달라진다 — 비정기 리듬 (해시 분산)."""
    params = default_params()
    monday = day_moments("a_probe_office", "flexible", datetime(2026, 3, 2, tzinfo=UTC), params)
    tuesday = day_moments("a_probe_office", "flexible", datetime(2026, 3, 3, tzinfo=UTC), params)
    assert set(monday) != set(tuesday)


# ---- phases 배선 — due가 아닌 액터의 포스팅 모먼트 (PG+Redis, 스텁 AI) ----

WORLD = "w_rhythm"
CLOCK = TickClock(genesis=datetime(2026, 3, 1, tzinfo=UTC))
POSTER = "a_probe_poster"


def make_poster() -> Persona:
    """즉석 조립 캐릭터 — 특정 인물 하드코딩 없음 (사용자 표준)."""
    return Persona(
        id=POSTER,
        name="기록자",
        archetype="probe_poster",
        identity_core="일상을 곧잘 세상에 남기는 주민.",
        needs_bias={"belonging": 0.8, "achievement": 0.5},
        lifestyle="flexible",
    )


def rhythm_only_tick(actor_id: str, lifestyle: str) -> int:
    """포스팅 모먼트이면서 LOD(Cold) due는 아닌 tick — 리듬 경로만 발화한다."""
    params = default_params()
    lod = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    for tick in range(TICKS_PER_DAY):
        if posting_moment(
            actor_id, lifestyle, CLOCK.world_time_at(tick), params
        ) and not is_due(actor_id, lod, tick):
            return tick
    raise AssertionError("포스팅 모먼트 tick을 찾지 못했다 — 파라미터를 확인하라")


class _PostingStubAi:
    """포스팅 모먼트의 LLM decide를 재현하는 스텁 — 컨텍스트를 기록한다."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.bundles: list[str] = []

    async def decide_action(self, bundle, schema, *, tier, actor_id, tick):
        self.bundles.append(bundle.user)
        if self._fail:
            return None
        return {
            "action_kind": "share",
            "intent": "오늘 있었던 일을 짧은 근황으로 남긴다",
            "target_actor_id": "a_probe_ghost",  # 환각 대상 — sanitize가 끊어야 한다
            "location_id": None,
            "params": {},
            "decision_trace": {"trace_id": f"stub-{actor_id}-{tick}", "tier": tier},
        }

    async def converse(self, *args, **kwargs):
        return None

    async def reflect(self, *args, **kwargs):
        return None


async def test_rhythm_moment_runs_llm_decide_for_non_due_actor(conn, redis):
    """due가 아닌 액터도 포스팅 모먼트 tick엔 LLM decide 경로로 행동을 남긴다."""
    persona = make_poster()
    ai = _PostingStubAi()
    phases = ActorPhases([persona], ai=ai, memory=WorkingMemory(redis))
    lod = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    phases._lods[POSTER] = lod
    tick = rhythm_only_tick(POSTER, persona.lifestyle)

    await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=0)

    [action] = await read_stream(conn, WORLD, "actor", POSTER)
    payload = action.envelope["payload"]
    assert payload["action_kind"] == "share"
    assert payload["target_actor_id"] is None  # 환각 대상은 소스에서 끊겼다
    # 컨텍스트에 '근황을 남기고 싶은 순간' 결의 힌트가 들어갔다
    assert any("근황" in bundle for bundle in ai.bundles)
    # decided 카운트 — tick.completed 스키마는 hot/warm/cold로 닫혀 있다 → warm 합산
    events = await read_stream(conn, WORLD, "system", "tick")
    decided = events[-1].envelope["payload"]["actors_decided"]
    assert decided == {"hot": 0, "warm": 1, "cold": 0}
    # 자기 발화는 관심 신호가 아니다 — LOD 불변 (승격/강등/touch 없음)
    assert phases._lods[POSTER] == lod


async def test_rhythm_moment_skips_silently_on_ai_failure(conn, redis):
    """포스팅 모먼트의 AI 실패는 조용히 생략 — 규칙 문장으로 피드를 도배하지 않는다."""
    persona = make_poster()
    ai = _PostingStubAi(fail=True)
    phases = ActorPhases([persona], ai=ai, memory=WorkingMemory(redis))
    lod = ActorLod(tier=Tier.COLD, last_interest_tick=0)
    phases._lods[POSTER] = lod
    tick = rhythm_only_tick(POSTER, persona.lifestyle)

    head = await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=0)

    assert ai.bundles  # LLM decide는 시도됐다
    assert head == 2  # started + completed 뿐 — 행동이 남지 않았다
    assert await read_stream(conn, WORLD, "actor", POSTER) == []
    events = await read_stream(conn, WORLD, "system", "tick")
    decided = events[-1].envelope["payload"]["actors_decided"]
    assert decided == {"hot": 0, "warm": 0, "cold": 0}
    assert phases._lods[POSTER] == lod  # LOD 불변


async def test_due_actor_does_not_double_act_on_moment_tick(conn, redis):
    """이번 tick에 이미 결정한(due) 액터는 리듬 경로를 다시 돌지 않는다 — 행동 1개."""
    persona = make_poster()
    ai = _PostingStubAi()
    phases = ActorPhases([persona], ai=ai, memory=WorkingMemory(redis))
    params = default_params()
    tick = next(
        t for t in range(TICKS_PER_DAY)
        if posting_moment(POSTER, persona.lifestyle, CLOCK.world_time_at(t), params)
    )
    # 관심이 살아있는 Hot — 유휴 강등 없이 이 tick에 due다
    phases._lods[POSTER] = ActorLod(tier=Tier.HOT, last_interest_tick=tick)

    await run_tick(conn, phases, CLOCK, WORLD, tick=tick, head=0)

    actions = await read_stream(conn, WORLD, "actor", POSTER)
    assert len(actions) == 1  # due 결정 하나뿐 — 리듬 중복 없음
    events = await read_stream(conn, WORLD, "system", "tick")
    decided = events[-1].envelope["payload"]["actors_decided"]
    assert decided == {"hot": 1, "warm": 0, "cold": 0}
