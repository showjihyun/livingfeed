"""Director 서비스 — 관찰은 매 tick 규칙, 개입은 임계·예산 안에서만 (ADR-013).

소비:
- LF_ACTOR(해당 세계) — drama 신호 수집 (관찰은 읽기 전용)
- LF_SYS(해당 세계) — tick.completed가 평가 경계, 자기 intervened로 예산 복원
발행: append(world.incident.occurred / system.director.intervened) →
outbox relay 경유 (ADR-017). permissions.yaml이 world.*/system.director.* 밖을
스키마 레벨에서 거부한다 — hard rule의 집행 지점.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from collections import deque
from typing import Any

import nats
import nats.errors
from lf_eventstore import NewEvent, append, current_head, new_ulid
from lf_projector.graph_api import GraphQueryClient
from psycopg import AsyncConnection

from lf_director.client import DirectorAiClient
from lf_director.config import Config
from lf_director.planner import (
    ARC_SYSTEM,
    DIRECTOR_SYSTEM,
    SEASON_SYSTEM,
    arc_from_plan,
    arc_schema,
    build_arc_user,
    build_plan_user,
    build_season_user,
    intervention_from_plan,
    plan_schema,
    season_from_plan,
    season_schema,
)
from lf_director.retrospect import season_retrospective, tune_pacing
from lf_director.rules import (
    ARC_TOOL,
    SEASON_TOOL,
    SEASON_TYPE,
    TOOL_STREAK_LIMIT,
    BudgetState,
    Intervention,
    decide,
    is_fireable,
)
from lf_director.signals import DramaWindow, default_params

logger = logging.getLogger("lf.director")

PRINCIPAL = "engine.director"
AUDIT_TYPE = "system.director.intervened"

#: 감사 스트림 파티션 — Director는 세계당 1 인스턴스라 CAS 경합이 없다.
#: 산출 world.* 이벤트의 타입/파티션은 Intervention이 도구별로 안다 (rules/planner).
AUDIT_STREAM_KEY = "director"


class Director:
    def __init__(
        self,
        cfg: Config,
        *,
        params: dict[str, Any] | None = None,
        ai_client: DirectorAiClient | None = None,
        names: dict[str, str] | None = None,
    ) -> None:
        self._cfg = cfg
        base = copy.deepcopy(params or default_params())
        if cfg.quiet_ticks_override is not None:
            base["observation"]["quiet_ticks_to_fire"] = cfg.quiet_ticks_override
        if cfg.quiet_threshold_override is not None:
            base["observation"]["quiet_threshold"] = cfg.quiet_threshold_override
        self._params = base
        self._incidents = base["incidents"]
        self._themes = base.get("season_themes", [])
        self._window = DramaWindow(self._params)
        self._budget = BudgetState()
        self._seen_audits: set[str] = set()
        #: 개입 선택 LLM 경로 — None이면 규칙 decide만 (dev 기본·replay). run()이 배선한다
        self._ai = ai_client
        self._names = names or {}
        #: 시즌 계획(저빈도)의 상태 — 현재 테마, 마지막으로 계획한 시즌-일 (ADR-013)
        self._current_theme: str | None = None
        self._last_season_day = -1
        #: 최근 드라마 도구 이력 (오래된 → 최근) — 다양성 힌트: 프롬프트에 주입해
        #: 같은 도구 연속 반복을 피하게 하고, 감사 signals에 남긴다. 자기 감사
        #: 재소비(_restore_budget)로 재시작에도 흐름이 복원된다
        self._recent_tools: deque[str] = deque(maxlen=5)
        #: 최근 사건 종류 이력 — 도구 아래 층위의 다양성 (같은 소문·정전 반복 회피).
        #: 규칙 폴백은 직전 종류를 비키고, LLM 프롬프트에도 이력이 실린다
        self._recent_kinds: deque[str] = deque(maxlen=5)

    def _streak_capped_tool(self) -> str | None:
        """연속 사용 상한(TOOL_STREAK_LIMIT)에 닿은 도구 — 이번 선택지에서 제외된다.

        다양성 hard rule (ADR-013): 프롬프트 힌트(감점)를 넘어 스키마 enum에서
        빠진다. 다른 도구가 한 번이라도 끼면 streak가 끊겨 다시 허용된다.
        """
        if len(self._recent_tools) < TOOL_STREAK_LIMIT:
            return None
        tail = list(self._recent_tools)[-TOOL_STREAK_LIMIT:]
        return tail[-1] if len(set(tail)) == 1 else None

    async def _select(
        self, snapshot: Any, tension: list[list[Any]]
    ) -> Intervention | None:
        """개입을 고른다 — LLM 맥락 선택을 시도하고, 없거나 실패하면 규칙 폴백.

        LLM 호출은 반드시 발화 게이트를 통과한 뒤에만 일어난다(비용·hard rule 선행).
        LLM이 화이트리스트 밖(연속 상한 제외 도구 포함)을 고르면 폴백이다 —
        LLM은 행동 반경을 넓힐 수 없다.
        """
        capped = self._streak_capped_tool()
        if self._ai is not None and is_fireable(snapshot, self._budget, self._params):
            schema = plan_schema(
                [inc["kind"] for inc in self._incidents], exclude_tool=capped
            )
            user = build_plan_user(
                snapshot, tension, self._incidents, self._names,
                recent_tools=list(self._recent_tools),
                recent_kinds=list(self._recent_kinds),
                excluded_tool=capped,
            )
            output, model = await self._ai.plan_intervention(
                DIRECTOR_SYSTEM, user, schema,
                world_id=self._cfg.world_id, tick=snapshot.tick,
            )
            if output is not None:
                chosen = intervention_from_plan(
                    output, snapshot, tension, self._incidents, self._names, model=model
                )
                # 제외 도구를 그래도 골랐다면 무효 — 방어적 재집행 (스키마가 1차 방어)
                if chosen is not None and chosen.tool != capped:
                    return chosen
            # LLM 실패/무효 → 규칙 폴백 (세계는 계속 돈다, ADR-013 단계적 도입)
        return decide(
            snapshot, self._budget, tension, params=self._params,
            avoid_kind=self._recent_kinds[-1] if self._recent_kinds else None,
            names=self._names,
        )

    async def _append_intervention(
        self, conn: AsyncConnection, snapshot: Any,
        intervention: Intervention, budget_remaining: int,
    ) -> None:
        """감사 선행 + 산출 이벤트 적재 — 도구·드라마/시즌 불문 공통 (ADR-013).

        예산 소비·quiet 리셋은 호출자의 몫이다 (드라마 개입만 예산을 쓴다).
        """
        cfg = self._cfg
        audit_id = new_ulid()
        head = await current_head(conn, cfg.world_id, "system", AUDIT_STREAM_KEY)
        await append(
            conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=cfg.world_id, stream="system", stream_key=AUDIT_STREAM_KEY,
                    type=AUDIT_TYPE, tick=snapshot.tick, event_id=audit_id,
                    payload={
                        "tool": intervention.tool,
                        "reason": intervention.reason,
                        "signals": intervention.signals,
                        "target_correlation_id": None,
                        "budget_remaining": max(0, budget_remaining),
                    },
                )
            ],
            expected_head=head,
        )
        # 산출 이벤트 — 도구별 스트림/타입/파티션/payload는 Intervention이 안다.
        head = await current_head(conn, cfg.world_id, intervention.stream, intervention.stream_key)
        await append(
            conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=cfg.world_id, stream=intervention.stream,
                    stream_key=intervention.stream_key, type=intervention.event_type,
                    tick=snapshot.tick, causation_id=audit_id, correlation_id=audit_id,
                    payload=intervention.payload,
                )
            ],
            expected_head=head,
        )
        self._seen_audits.add(audit_id)
        logger.info(
            "개입[%s]: %s → %s tick=%d — %s",
            intervention.signals.get("selector", "rule"),
            intervention.tool, intervention.event_type, snapshot.tick, intervention.reason,
        )

    async def evaluate(
        self, conn: AsyncConnection, snapshot: Any, graph: GraphQueryClient | None
    ) -> bool:
        """드라마 게이트 평가 — 침체 시 순간적 개입 하나 (예산·quiet 소비). 개입하면 True."""
        cfg = self._cfg
        tension = (
            await graph.tension_pairs(cfg.world_id) if graph is not None else []
        )
        intervention = await self._select(snapshot, tension)
        if intervention is None:
            return False
        # 다양성 메트릭 — 최근 도구 흐름과 연속 사용 횟수(repeat_streak)를 감사에
        # 남긴다. streak 1이 다양, 커질수록 기계적이다 (리플레이 튜닝의 정량 지표)
        streak = 1
        for prev in reversed(self._recent_tools):
            if prev != intervention.tool:
                break
            streak += 1
        intervention.signals["repeat_streak"] = streak
        if self._recent_tools:
            intervention.signals["recent_tools"] = list(self._recent_tools)
        kind = intervention.payload.get("incident_kind")
        if kind is not None:
            intervention.signals["incident_kind"] = kind  # 재시작 복원용 (감사가 원천)
        remaining_after = self._budget.remaining(snapshot.tick, self._params) - 1
        await self._append_intervention(conn, snapshot, intervention, remaining_after)
        self._budget.record(snapshot.tick, None)
        self._window.reset_quiet()
        self._recent_tools.append(intervention.tool)
        if kind is not None:
            self._recent_kinds.append(kind)
        return True

    async def plan_season(self, conn: AsyncConnection, snapshot: Any) -> bool:
        """저빈도 시즌 계획 — 세계 하루 1회, 드라마 게이트·예산과 분리 (ADR-013).

        LLM이 시즌 테마를 정하면 season_set을 적재한다(Director가 자기 이벤트를 소비해
        페이싱 파라미터를 갱신). 규칙 프로바이더는 시즌 계획을 지원하지 않으므로 dev
        기본에서는 시즌이 바뀌지 않는다 (테마 미변경). 예산을 쓰지 않는다.
        """
        if self._ai is None or not self._themes:
            return False
        theme_names = [t["name"] for t in self._themes]
        schema = season_schema(theme_names)
        user = build_season_user(snapshot, self._themes, self._current_theme)
        output, model = await self._ai.plan_intervention(
            SEASON_SYSTEM, user, schema, world_id=self._cfg.world_id, tick=snapshot.tick,
        )
        if output is None:
            return False
        intervention = season_from_plan(output, snapshot, self._themes, model=model)
        if intervention is None:
            return False
        # 시즌은 드라마 예산과 무관 — 남은 예산을 그대로 감사에 싣는다(소비 없음)
        await self._append_intervention(
            conn, snapshot, intervention, self._budget.remaining(snapshot.tick, self._params)
        )
        return True

    async def plan_arcs(self, conn: AsyncConnection, snapshot: Any) -> bool:
        """저빈도 인생 아크 계획 — 정체된 인물 하나에게 인생 단계+방향을 준다 (ADR-013,
        docs/plan/08). 시즌 계획과 같은 저빈도 케이던스, 드라마 예산과 분리.

        로스터(read.actors 대신 personas 이름)가 있어야 한다. LLM이 대상·단계·방향을
        고르면 arc_planned를 적재한다 — Actor Runtime이 소비해 아크를 저장·주입한다.
        """
        if self._ai is None or not self._names:
            return False
        roster = sorted(self._names.items())
        schema = arc_schema([aid for aid, _ in roster])
        user = build_arc_user(snapshot, roster)
        output, model = await self._ai.plan_intervention(
            ARC_SYSTEM, user, schema, world_id=self._cfg.world_id, tick=snapshot.tick,
        )
        if output is None:
            return False
        intervention = arc_from_plan(output, snapshot, set(self._names), model=model)
        if intervention is None:
            return False
        # 아크 계획도 드라마 예산과 무관 — 소비 없이 감사·산출만 적재
        await self._append_intervention(
            conn, snapshot, intervention, self._budget.remaining(snapshot.tick, self._params)
        )
        return True

    async def review_season(self, conn: AsyncConnection, day: int) -> dict[str, Any] | None:
        """지난 시즌-일의 회고 — 구조화 로그로 남긴다 (ADR-013 후속).

        회고는 이벤트의 파생물이라 이벤트로 적재하지 않는다 — 리플레이
        (season_retrospective)로 언제든 재생성된다. 원본/파생 경계를 지킨다.
        실패해도 연출은 계속된다 (회고는 눈이지 손이 아니다).
        """
        try:
            report = await season_retrospective(
                conn, self._cfg.world_id, day,
                interval_ticks=self._cfg.season_interval_ticks,
            )
        except Exception:
            logger.exception("시즌 회고 실패 — 연출은 계속된다 (day=%d)", day)
            return None
        logger.info(
            "시즌 회고 day=%d: %s", day, json.dumps(report, ensure_ascii=False)
        )
        # 회고 → 페이싱 자기 조정 (P 제어) — 명시 override(테스트·운영 고정)면 손대지 않는다
        if self._cfg.quiet_ticks_override is None:
            current = int(self._params["observation"]["quiet_ticks_to_fire"])
            adjusted, reason = tune_pacing(report, current, self._params)
            if reason is not None:
                self._params["observation"]["quiet_ticks_to_fire"] = adjusted
                logger.info(
                    "페이싱 자기 조정: quiet_ticks_to_fire %d → %d (%s)",
                    current, adjusted, reason,
                )
        return report

    def _restore_budget(self, envelope: dict[str, Any]) -> None:
        """자기 감사 이벤트 재소비 → 예산 복원 (자기가 방금 적재한 것은 중복 방지).

        재시작 시 이미 ack된 과거 개입은 복원되지 않는다 — 최악의 경우 재시작
        직후 한 창(세계 1시간)에서 예산이 초과될 수 있는 정도로, MVP 허용 오차다.
        """
        if envelope["event_id"] in self._seen_audits:
            return
        # 시즌·아크 계획은 저빈도 구조적 결정 — 드라마 예산과 무관하다 (ADR-013)
        if envelope["payload"].get("tool") in (SEASON_TOOL, ARC_TOOL):
            return
        # 예산과 함께 최근 도구·사건 종류 흐름도 감사에서 되찾는다 (다양성 힌트 복원)
        self._recent_tools.append(envelope["payload"]["tool"])
        kind = (envelope["payload"].get("signals") or {}).get("incident_kind")
        if kind is not None:
            self._recent_kinds.append(kind)
        self._budget.record(
            envelope["tick"], envelope["payload"].get("target_correlation_id")
        )

    def _apply_season(self, envelope: dict[str, Any]) -> None:
        """자기 season_set 이벤트를 소비해 시즌 페이싱 파라미터를 갱신한다 (ADR-013).

        시즌 상태는 이벤트에서 복원된다. 재시작 시 durable이 지난 이벤트를 다시 읽지
        않으므로 마지막 테마가 유실될 수 있다 — budget 복원과 같은 MVP 허용 오차다.
        갱신 노브는 개입 빈도(갈등 빈도)뿐이라 DramaWindow(quiet_threshold)는 무관하다.
        """
        p = envelope["payload"]
        self._current_theme = p["theme"]
        self._params["observation"]["quiet_ticks_to_fire"] = int(p["quiet_ticks_to_fire"])
        self._params["budget"]["max_interventions"] = int(p["max_interventions"])
        logger.info(
            "시즌 테마: %s — quiet_to_fire=%d max_interventions=%d (tick=%d)",
            p["theme"], p["quiet_ticks_to_fire"], p["max_interventions"], envelope["tick"],
        )

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        nc = await nats.connect(cfg.nats_url)
        async with await AsyncConnection.connect(cfg.pg_dsn, autocommit=True) as conn:
            try:
                js = nc.jetstream()
                graph = GraphQueryClient(nc, cfg.env)
                # LLM 개입 선택 배선 — 명시 주입(테스트)이 없고 켜져 있을 때만.
                # rule 프로바이더면 director_plan이 미지원이라 자동 규칙 폴백된다.
                if self._ai is None and cfg.llm_selection:
                    self._ai = DirectorAiClient(nc, cfg.env)
                observe = await js.pull_subscribe(
                    f"lf.{cfg.env}.{cfg.world_id}.actor.>",
                    durable=cfg.observe_durable, stream="LF_ACTOR",
                )
                system = await js.pull_subscribe(
                    f"lf.{cfg.env}.{cfg.world_id}.system.>",
                    durable=cfg.sys_durable, stream="LF_SYS",
                )
                logger.info(
                    "director 대기 — world=%s quiet_to_fire=%s 개입선택=%s",
                    cfg.world_id, self._params["observation"]["quiet_ticks_to_fire"],
                    "LLM+규칙폴백" if self._ai is not None else "규칙",
                )
                while not stop.is_set():
                    # 관찰 먼저 비우고 tick 경계를 처리한다 (신호가 경계에 선행)
                    for psub, handler in ((observe, "observe"), (system, "system")):
                        try:
                            msgs = await psub.fetch(
                                cfg.batch_size, timeout=cfg.fetch_timeout_s
                            )
                        except (TimeoutError, nats.errors.TimeoutError):
                            continue
                        for msg in msgs:
                            try:
                                envelope = json.loads(msg.data)
                                if handler == "observe":
                                    self._window.observe(envelope)
                                elif envelope["type"] == "system.tick.completed":
                                    snapshot = self._window.close_tick(envelope["tick"])
                                    # 시즌·아크 계획은 저빈도(세계 하루 1회) — 드라마
                                    # 게이트와 분리된 케이던스로 (ADR-013). 예산도 따로다.
                                    day = envelope["tick"] // cfg.season_interval_ticks
                                    if day > self._last_season_day:
                                        previous = self._last_season_day
                                        self._last_season_day = day
                                        if previous >= 0:
                                            # 지난 날을 돌아보고 새 날을 계획한다
                                            await self.review_season(conn, previous)
                                        await self.plan_season(conn, snapshot)
                                        await self.plan_arcs(conn, snapshot)
                                    await self.evaluate(conn, snapshot, graph)
                                elif envelope["type"] == AUDIT_TYPE:
                                    self._restore_budget(envelope)
                                elif envelope["type"] == SEASON_TYPE:
                                    self._apply_season(envelope)
                            except Exception:
                                # 관찰은 최선 노력이다 — 세계를 멈추지 않는다 (기록 후 전진)
                                logger.exception("관찰 처리 실패 — 건너뜀")
                            await msg.ack()
            finally:
                await nc.drain()
