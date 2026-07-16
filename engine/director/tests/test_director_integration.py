"""Director 개입 적재 통합 — 감사 선행·인과 연결·권한 (ADR-013).

PostgreSQL 필요 (없으면 skip — conftest 참고). 관찰 루프(JetStream)는
E2E 스모크가 검증한다 — 여기는 evaluate의 적재 계약이다.
"""

from lf_director.config import Config
from lf_director.director import Director
from lf_director.signals import Snapshot
from lf_eventstore import read_stream

WORLD = "w_test"


def make_director(quiet_override: int | None = None) -> Director:
    cfg = Config(
        pg_dsn="unused", nats_url="unused", env="test", world_id=WORLD,
        quiet_ticks_override=quiet_override,
    )
    return Director(cfg)


async def test_intervention_appends_audit_then_incident(conn):
    director = make_director()
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30), graph=None
    )
    assert fired

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["type"] == "system.director.intervened"
    assert audit["payload"]["tool"] == "inject_incident"
    assert "침체 감지" in audit["payload"]["reason"]

    [incident] = [s.envelope for s in await read_stream(conn, WORLD, "world", "incidents")]
    assert incident["type"] == "world.incident.occurred"
    # 산출물은 감사 기록을 가리킨다 — 서사가 감사 가능 (ADR-002/013)
    assert incident["causation_id"] == audit["event_id"]
    assert incident["correlation_id"] == audit["event_id"]
    assert 0 < incident["payload"]["intensity"] <= 1


async def test_below_threshold_means_no_intervention(conn):
    director = make_director()
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.5, quiet_ticks=0), graph=None
    )
    assert not fired
    assert await read_stream(conn, WORLD, "world", "incidents") == []


async def test_budget_limits_consecutive_interventions(conn):
    director = make_director()
    fired = 0
    for i in range(5):
        if await director.evaluate(
            conn, Snapshot(tick=120 + i, drama_ma=0.05, quiet_ticks=30 + i), graph=None
        ):
            fired += 1
    assert fired == 2  # 창(세계 1시간)당 상한 (hard rule)
    incidents = await read_stream(conn, WORLD, "world", "incidents")
    assert len(incidents) == 2


# --- Phase 2: LLM 개입 선택 (스텁 클라이언트로 배선·하드룰 재집행 검증) -----------


class _StubGraph:
    def __init__(self, pairs: list[list]) -> None:
        self._pairs = pairs

    async def tension_pairs(self, world_id: str, **_: object) -> list[list]:
        return self._pairs


class _StubAiClient:
    """canned director_plan 출력을 돌려주는 스텁 — 라이브 모델 없이 LLM 경로를 탄다."""

    def __init__(self, output: dict | None, model: str = "stub-model") -> None:
        self._output = output
        self._model = model
        self.calls: list[dict] = []

    async def plan_intervention(
        self, system: str, user: str, output_schema: dict, *, world_id: str, tick: int
    ):
        self.calls.append({"user": user, "schema": output_schema, "tick": tick})
        return self._output, self._model


def make_llm_director(ai: _StubAiClient) -> Director:
    cfg = Config(pg_dsn="unused", nats_url="unused", env="test", world_id=WORLD)
    return Director(
        cfg, ai_client=ai, names={"a_minji_kim": "김민지", "a_seongho_park": "박성호"}
    )


async def test_llm_selection_places_contextual_incident(conn):
    tension = [["a_minji_kim", "a_seongho_park", 0.8, 0.2]]
    plan = {
        "tool": "inject_incident",
        "incident_kind": "rumor_spread",
        "affected_actor_ids": ["a_minji_kim"],
        "description": "박성호를 둘러싼 말이 김민지에게까지 닿았다",
        "intensity": 0.72,
        "rationale": "둘의 원한이 가장 높다 — 소문이 불씨가 된다",
    }
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30), _StubGraph(tension)
    )
    assert fired
    # 모델이 호출됐고 프롬프트가 이름으로 그라운딩됐다
    assert ai.calls and "김민지" in ai.calls[0]["user"]

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["signals"]["selector"] == "llm"  # 감사에 선택 주체 기록
    assert audit["payload"]["signals"]["model"] == "stub-model"
    assert audit["payload"]["reason"] == plan["rationale"]

    [incident] = [s.envelope for s in await read_stream(conn, WORLD, "world", "incidents")]
    p = incident["payload"]
    assert p["incident_kind"] == "rumor_spread"       # LLM이 고른 종류
    assert p["description"] == plan["description"]      # LLM의 맥락 서술 보존
    assert p["affected_actor_ids"] == ["a_minji_kim"]  # 후보 안에서만
    assert p["location_id"] == "loc_newsroom"          # 라이브러리에서만 (rumor_spread)
    assert p["intensity"] == 0.72


async def test_llm_invalid_kind_falls_back_to_rule(conn):
    # 화이트리스트 밖 사건 → intervention_from_plan None → 규칙 decide 폴백
    ai = _StubAiClient(
        {"tool": "inject_incident", "incident_kind": "earthquake", "affected_actor_ids": [],
         "description": "지진", "intensity": 0.9, "rationale": "x"}
    )
    director = make_llm_director(ai)
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30), graph=None
    )
    assert fired  # 폴백으로 개입은 일어난다 — 세계는 계속 돈다
    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["signals"]["selector"] == "rule"


async def test_llm_not_called_below_threshold(conn):
    # 발화 게이트 미통과 → 모델 호출 없음 (비용·hard rule이 LLM에 선행)
    ai = _StubAiClient(
        {"tool": "inject_incident", "incident_kind": "rumor_spread", "affected_actor_ids": [],
         "description": "x", "intensity": 0.5, "rationale": "y"}
    )
    director = make_llm_director(ai)
    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.5, quiet_ticks=0), graph=None
    )
    assert not fired
    assert ai.calls == []
    assert await read_stream(conn, WORLD, "world", "incidents") == []


async def test_llm_nudge_surfaces_private_observation(conn):
    # 다른 도구 선택: nudge_perception → world.observation.surfaced (비공개, 피드 승격 안 됨)
    tension = [["a_minji_kim", "a_seongho_park", 0.8, 0.2]]
    plan = {
        "tool": "nudge_perception",
        "target_actor_id": "a_minji_kim",
        "observation": "박성호의 메모에서 앞뒤 안 맞는 대목을 우연히 봤다",
        "about_actor_id": "a_seongho_park",
        "rationale": "공개 소동 없이 민지의 다음 선택을 흔든다",
    }
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    fired = await director.evaluate(
        conn, Snapshot(tick=121, drama_ma=0.05, quiet_ticks=30), _StubGraph(tension)
    )
    assert fired

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["tool"] == "nudge_perception"  # 감사에 도구 기록
    assert audit["payload"]["signals"]["selector"] == "llm"

    [obs] = [s.envelope for s in await read_stream(conn, WORLD, "world", "observations")]
    assert obs["type"] == "world.observation.surfaced"
    assert obs["payload"]["target_actor_id"] == "a_minji_kim"
    assert obs["payload"]["observation"] == plan["observation"]
    assert obs["payload"]["about_actor_id"] == "a_seongho_park"
    # 사적 관측 — 공개 사건 스트림에는 들어가지 않는다
    assert await read_stream(conn, WORLD, "world", "incidents") == []


async def test_llm_promote_spotlights_actor_on_system_stream(conn):
    # 세 번째 도구: promote_actor → system.director.spotlighted (제어 신호, world 아님)
    tension = [["a_minji_kim", "a_seongho_park", 0.8, 0.2]]
    plan = {
        "tool": "promote_actor",
        "target_actor_id": "a_seongho_park",
        "rationale": "박성호를 무대 중앙으로 — 더 자주 움직이게",
    }
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    fired = await director.evaluate(
        conn, Snapshot(tick=122, drama_ma=0.05, quiet_ticks=30), _StubGraph(tension)
    )
    assert fired

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["tool"] == "promote_actor"

    [spot] = [s.envelope for s in await read_stream(conn, WORLD, "system", "spotlight")]
    assert spot["type"] == "system.director.spotlighted"
    assert spot["payload"] == {"target_actor_id": "a_seongho_park"}
    # 세계 사건이 아니다 — world 스트림에는 없다
    assert await read_stream(conn, WORLD, "world", "incidents") == []
    assert await read_stream(conn, WORLD, "world", "observations") == []


async def test_plan_season_records_on_system_stream_without_budget(conn):
    # 저빈도 시즌 계획 — 드라마 게이트·예산과 분리된 별도 경로 (ADR-013)
    plan = {"season_theme": "turmoil", "rationale": "세계가 격동으로 접어들 때다"}
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    planned = await director.plan_season(
        conn, Snapshot(tick=360, drama_ma=0.05, quiet_ticks=30)
    )
    assert planned

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["tool"] == "set_season_theme"

    [season] = [s.envelope for s in await read_stream(conn, WORLD, "system", "season")]
    assert season["type"] == "system.director.season_set"
    assert season["payload"]["theme"] == "turmoil"
    assert season["payload"]["quiet_ticks_to_fire"] == 12  # 라이브러리 해석값
    assert season["payload"]["max_interventions"] == 4
    # 시즌 계획은 드라마 예산을 쓰지 않는다 (분리된 케이던스)
    assert director._budget.interventions_total == 0
    assert await read_stream(conn, WORLD, "world", "incidents") == []


async def test_consecutive_interventions_carry_recent_tools(conn):
    """개입 다양성 — 두 번째 개입부터 LLM 프롬프트에 최근 도구 이력이 주입되고,
    감사 signals에 recent_tools가 남는다 (연속 사용이 관찰 가능해진다)."""
    tension = [["a_minji_kim", "a_seongho_park", 0.8, 0.2]]
    plan = {
        "tool": "inject_incident",
        "incident_kind": "rumor_spread",
        "affected_actor_ids": ["a_minji_kim"],
        "description": "박성호를 둘러싼 말이 김민지에게까지 닿았다",
        "intensity": 0.7,
        "rationale": "둘의 원한이 가장 높다",
    }
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    assert await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30), _StubGraph(tension)
    )
    assert "최근 개입 이력" not in ai.calls[0]["user"]  # 첫 개입 — 이력 없음

    assert await director.evaluate(
        conn, Snapshot(tick=125, drama_ma=0.05, quiet_ticks=35), _StubGraph(tension)
    )
    assert "최근 개입 이력" in ai.calls[1]["user"]  # 두 번째 — 흐름이 보인다
    assert "inject_incident" in ai.calls[1]["user"]
    assert "사건 종류: rumor_spread" in ai.calls[1]["user"]  # 종류 층위 이력도

    audits = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert "recent_tools" not in audits[0]["payload"]["signals"]
    assert audits[0]["payload"]["signals"]["repeat_streak"] == 1  # 첫 개입 — 반복 아님
    assert audits[1]["payload"]["signals"]["recent_tools"] == ["inject_incident"]
    assert audits[1]["payload"]["signals"]["repeat_streak"] == 2  # 같은 도구 2연속 (정량)
    assert audits[1]["payload"]["signals"]["incident_kind"] == "rumor_spread"


def test_streak_capped_tool_detection():
    # 3연속이면 상한 — 다른 도구가 한 번이라도 끼면 streak가 끊긴다
    director = make_director()
    assert director._streak_capped_tool() is None  # 이력이 짧다
    director._recent_tools.extend(["inject_incident"] * 3)
    assert director._streak_capped_tool() == "inject_incident"
    director._recent_tools.append("nudge_perception")
    assert director._streak_capped_tool() is None  # 끊겼다 — 다시 허용


async def test_streak_cap_excludes_tool_from_schema_and_reenforces(conn):
    """다양성 hard rule — 상한에 걸린 도구는 LLM 스키마에서 빠지고, 그래도
    골라오면 무효(규칙 폴백)다. LLM은 행동 반경을 넓힐 수 없다 (ADR-013)."""
    tension = [["a_minji_kim", "a_seongho_park", 0.8, 0.2]]
    plan = {
        "tool": "inject_incident",  # 상한에 걸린 도구를 고집하는 LLM
        "incident_kind": "rumor_spread",
        "affected_actor_ids": ["a_minji_kim"],
        "description": "또 소문이다",
        "intensity": 0.5,
        "rationale": "같은 걸 또",
    }
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    director._recent_tools.extend(["inject_incident"] * 3)

    fired = await director.evaluate(
        conn, Snapshot(tick=120, drama_ma=0.05, quiet_ticks=30), _StubGraph(tension)
    )
    assert fired  # 세계는 계속 돈다 — 규칙 폴백으로
    # 스키마에서 상한 도구가 빠졌고, 프롬프트에도 고지됐다
    tools = ai.calls[0]["schema"]["properties"]["tool"]["enum"]
    assert "inject_incident" not in tools
    assert "연속 사용 상한" in ai.calls[0]["user"]
    # 그래도 골라온 LLM 선택은 무효 — 규칙 경로가 개입했다 (감사에 주체 기록)
    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["signals"]["selector"] == "rule"


def test_restore_budget_rebuilds_recent_tools_from_audits():
    # 재시작 후 남의(과거) 감사 재소비 → 예산과 함께 도구·사건 종류 흐름도 복원된다.
    # 시즌·아크 감사는 드라마 흐름이 아니다 — 이력에 안 들어간다
    director = make_director()
    director._restore_budget(
        {"event_id": "a", "tick": 5,
         "payload": {"tool": "inject_incident", "target_correlation_id": None,
                     "signals": {"incident_kind": "rumor_spread"}}}
    )
    director._restore_budget(
        {"event_id": "b", "tick": 6, "payload": {"tool": "set_season_theme"}}
    )
    director._restore_budget(
        {"event_id": "c", "tick": 7,
         "payload": {"tool": "nudge_perception", "target_correlation_id": None}}
    )
    assert list(director._recent_tools) == ["inject_incident", "nudge_perception"]
    assert list(director._recent_kinds) == ["rumor_spread"]  # 종류는 사건 감사에서만


async def test_plan_arcs_records_on_system_stream_without_budget(conn):
    # 저빈도 인생 아크 계획 — 시즌과 같은 케이던스, 드라마 예산과 분리 (ADR-013, plan/08)
    plan = {
        "target_actor_id": "a_minji_kim",
        "stage": "newcomer",
        "intention": "무대 뒤에서 나와 자기 이야기를 시작한다",
        "rationale": "인생이 가장 정체된 인물이다",
    }
    ai = _StubAiClient(plan)
    director = make_llm_director(ai)
    planned = await director.plan_arcs(
        conn, Snapshot(tick=360, drama_ma=0.05, quiet_ticks=30)
    )
    assert planned
    # 프롬프트가 로스터 이름으로 그라운딩됐다
    assert ai.calls and "김민지" in ai.calls[0]["user"]

    [audit] = [s.envelope for s in await read_stream(conn, WORLD, "system", "director")]
    assert audit["payload"]["tool"] == "plan_arc"

    [arc] = [s.envelope for s in await read_stream(conn, WORLD, "system", "arc")]
    assert arc["type"] == "system.director.arc_planned"
    assert arc["payload"] == {
        "target_actor_id": "a_minji_kim", "stage": "newcomer",
        "intention": "무대 뒤에서 나와 자기 이야기를 시작한다",
    }
    assert arc["causation_id"] == audit["event_id"]  # 산출물은 감사를 가리킨다
    # 아크 계획은 드라마 예산을 쓰지 않는다 — 세계 사건도 아니다
    assert director._budget.interventions_total == 0
    assert await read_stream(conn, WORLD, "world", "incidents") == []


async def test_plan_arcs_rejects_hallucinated_target(conn):
    # 로스터 밖 인물은 arc_from_plan에서 끊긴다 — 아무것도 적재되지 않는다
    plan = {
        "target_actor_id": "a_ghost", "stage": "newcomer",
        "intention": "존재하지 않는 인물의 이야기", "rationale": "x",
    }
    director = make_llm_director(_StubAiClient(plan))
    planned = await director.plan_arcs(
        conn, Snapshot(tick=360, drama_ma=0.05, quiet_ticks=30)
    )
    assert not planned
    assert await read_stream(conn, WORLD, "system", "arc") == []
    assert await read_stream(conn, WORLD, "system", "director") == []


async def test_review_season_folds_previous_day_as_log(conn):
    """시즌 회고 자동화 — 지난 날의 연출을 접어 리포트를 돌려준다 (이벤트가 아니라
    로그: 파생물은 리플레이로 재생성 가능하니 원본/파생 경계를 지킨다)."""
    director = make_director()
    # day 0의 개입 두 건 (tick 10, 130 — 예산 창 분리)
    assert await director.evaluate(
        conn, Snapshot(tick=10, drama_ma=0.05, quiet_ticks=30), graph=None
    )
    assert await director.evaluate(
        conn, Snapshot(tick=130, drama_ma=0.05, quiet_ticks=30), graph=None
    )
    report = await director.review_season(conn, 0)
    assert report is not None
    assert report["tick_range"] == [0, 360]
    assert report["interventions"]["total"] == 2
    assert report["interventions"]["by_selector"] == {"rule": 2}
    # day 1은 조용했다 — 빈 리포트도 리포트다
    quiet = await director.review_season(conn, 1)
    assert quiet["interventions"]["total"] == 0


def test_apply_season_updates_pacing_params_and_current_theme():
    # 자기 season_set 이벤트 소비 → 개입 빈도 파라미터 + 현재 테마 갱신 (이벤트 소싱)
    director = make_director()
    director._apply_season(
        {"tick": 5, "payload": {
            "theme": "turmoil", "quiet_ticks_to_fire": 12, "max_interventions": 4,
        }}
    )
    assert director._params["observation"]["quiet_ticks_to_fire"] == 12
    assert director._params["budget"]["max_interventions"] == 4
    assert director._current_theme == "turmoil"


def test_season_audit_does_not_consume_drama_budget():
    # season·arc 감사 재소비는 드라마 예산으로 세지 않는다 (분리, ADR-013)
    director = make_director()
    director._restore_budget(
        {"event_id": "x", "tick": 5, "payload": {"tool": "set_season_theme"}}
    )
    director._restore_budget(
        {"event_id": "x2", "tick": 5, "payload": {"tool": "plan_arc"}}
    )
    assert director._budget.interventions_total == 0  # 둘 다 무시됨
    # 반면 드라마 도구 감사는 예산으로 센다
    director._restore_budget(
        {"event_id": "y", "tick": 5, "payload": {"tool": "inject_incident",
                                                 "target_correlation_id": None}}
    )
    assert director._budget.interventions_total == 1
