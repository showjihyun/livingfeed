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
from typing import Any

import nats
import nats.errors
from lf_eventstore import NewEvent, append, current_head, new_ulid
from lf_projector.graph_api import GraphQueryClient
from psycopg import AsyncConnection

from lf_director.client import DirectorAiClient
from lf_director.config import Config
from lf_director.planner import (
    DIRECTOR_SYSTEM,
    build_plan_user,
    intervention_from_plan,
    plan_schema,
)
from lf_director.rules import BudgetState, Intervention, decide, is_fireable
from lf_director.signals import DramaWindow, default_params

logger = logging.getLogger("lf.director")

PRINCIPAL = "engine.director"
INCIDENT_TYPE = "world.incident.occurred"
AUDIT_TYPE = "system.director.intervened"

#: 세계 스트림의 사건 파티션 — Director는 세계당 1 인스턴스라 CAS 경합이 없다
INCIDENT_STREAM_KEY = "incidents"
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
        self._window = DramaWindow(self._params)
        self._budget = BudgetState()
        self._seen_audits: set[str] = set()
        #: 개입 선택 LLM 경로 — None이면 규칙 decide만 (dev 기본·replay). run()이 배선한다
        self._ai = ai_client
        self._names = names or {}

    async def _select(
        self, snapshot: Any, tension: list[list[Any]]
    ) -> Intervention | None:
        """개입을 고른다 — LLM 맥락 선택을 시도하고, 없거나 실패하면 규칙 폴백.

        LLM 호출은 반드시 발화 게이트를 통과한 뒤에만 일어난다(비용·hard rule 선행).
        LLM이 화이트리스트 밖을 고르면 intervention_from_plan이 None → 규칙 폴백.
        """
        if self._ai is not None and is_fireable(snapshot, self._budget, self._params):
            schema = plan_schema([inc["kind"] for inc in self._incidents])
            user = build_plan_user(snapshot, tension, self._incidents, self._names)
            output, model = await self._ai.plan_intervention(
                DIRECTOR_SYSTEM, user, schema,
                world_id=self._cfg.world_id, tick=snapshot.tick,
            )
            if output is not None:
                chosen = intervention_from_plan(
                    output, snapshot, tension, self._incidents, self._names, model=model
                )
                if chosen is not None:
                    return chosen
            # LLM 실패/무효 → 규칙 폴백 (세계는 계속 돈다, ADR-013 단계적 도입)
        return decide(snapshot, self._budget, tension, params=self._params)

    async def evaluate(
        self, conn: AsyncConnection, snapshot: Any, graph: GraphQueryClient | None
    ) -> bool:
        """tick 경계 평가 — 개입하면 True. 감사 기록이 산출물보다 먼저 적재된다."""
        cfg = self._cfg
        tension = (
            await graph.tension_pairs(cfg.world_id) if graph is not None else []
        )
        intervention = await self._select(snapshot, tension)
        if intervention is None:
            return False

        audit_id = new_ulid()
        remaining_after = self._budget.remaining(snapshot.tick, self._params) - 1
        head = await current_head(conn, cfg.world_id, "system", AUDIT_STREAM_KEY)
        await append(
            conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=cfg.world_id,
                    stream="system",
                    stream_key=AUDIT_STREAM_KEY,
                    type=AUDIT_TYPE,
                    tick=snapshot.tick,
                    event_id=audit_id,
                    payload={
                        "tool": intervention.tool,
                        "reason": intervention.reason,
                        "signals": intervention.signals,
                        "target_correlation_id": None,
                        "budget_remaining": max(0, remaining_after),
                    },
                )
            ],
            expected_head=head,
        )
        head = await current_head(conn, cfg.world_id, "world", INCIDENT_STREAM_KEY)
        await append(
            conn, PRINCIPAL,
            [
                NewEvent(
                    world_id=cfg.world_id,
                    stream="world",
                    stream_key=INCIDENT_STREAM_KEY,
                    type=INCIDENT_TYPE,
                    tick=snapshot.tick,
                    causation_id=audit_id,
                    correlation_id=audit_id,  # 개입이 시작한 새 서사 사슬
                    payload={
                        "incident_kind": intervention.incident_kind,
                        "description": intervention.description,
                        "location_id": intervention.location_id,
                        "affected_actor_ids": intervention.affected_actor_ids,
                        "intensity": intervention.intensity,
                    },
                )
            ],
            expected_head=head,
        )
        self._budget.record(snapshot.tick, None)
        self._seen_audits.add(audit_id)
        self._window.reset_quiet()
        logger.info(
            "개입[%s]: %s(%s) tick=%d — %s",
            intervention.signals.get("selector", "rule"),
            intervention.tool, intervention.incident_kind, snapshot.tick, intervention.reason,
        )
        return True

    def _restore_budget(self, envelope: dict[str, Any]) -> None:
        """자기 감사 이벤트 재소비 → 예산 복원 (자기가 방금 적재한 것은 중복 방지).

        재시작 시 이미 ack된 과거 개입은 복원되지 않는다 — 최악의 경우 재시작
        직후 한 창(세계 1시간)에서 예산이 초과될 수 있는 정도로, MVP 허용 오차다.
        """
        if envelope["event_id"] in self._seen_audits:
            return
        self._budget.record(
            envelope["tick"], envelope["payload"].get("target_correlation_id")
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
                                    await self.evaluate(conn, snapshot, graph)
                                elif envelope["type"] == AUDIT_TYPE:
                                    self._restore_budget(envelope)
                            except Exception:
                                # 관찰은 최선 노력이다 — 세계를 멈추지 않는다 (기록 후 전진)
                                logger.exception("관찰 처리 실패 — 건너뜀")
                            await msg.ack()
            finally:
                await nc.drain()
