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
