"""kuzu-projector — LF_REL → Kuzu 그래프 갱신 + graph query API (ADR-006, ADR-003).

프로젝터 계약 준수:
1. 멱등 — 엣지 SET 덮어쓰기 (자연 멱등)
2. 체크포인트 — JetStream durable consumer
3. 재구축 — --rebuild 가 DB 디렉터리와 durable을 파괴 후 처음부터 재소비
4. 단방향 — 도메인 이벤트를 발행하지 않는다 (DLQ 이동은 인프라 경로)
5. 격리 — os-projector와 consumer 독립

Kuzu는 임베디드이므로 같은 프로세스가 graph query API(NATS request-reply)를
함께 노출한다 (ADR-006 §배치 구조).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import nats
from lf_dispatcher.subjects import dlq_subject
from nats.js.errors import NotFoundError
from psycopg import AsyncConnection

from lf_projector.config import Config
from lf_projector.consume import batches
from lf_projector.graph import RelGraph
from lf_projector.graph_api import serve_graph_api
from lf_projector.lag import KindMetrics, LagAggregator, observe
from lf_projector.replay import return_envelopes

logger = logging.getLogger("lf.projector.kuzu")

#: 은퇴 소멸 — 스튜디오 삭제의 이벤트화. 노드 + 양방향 간선을 지운다
RETIRED_TYPE = "actor.identity.retired"
#: 부활 재사영 — 복원의 이벤트화. es에서 그에 닿은 관계 역사를 다시 먹인다
RETURNED_TYPE = "actor.identity.returned"

HANDLERS = {
    "relationship.state.changed": RelGraph.apply_state_changed,
    "relationship.milestone.reached": RelGraph.apply_milestone,
    RETIRED_TYPE: RelGraph.apply_retired,
}


class KuzuProjector:
    def __init__(self, cfg: Config, metrics: KindMetrics | None = None) -> None:
        self._cfg = cfg
        self._graph = RelGraph(Path(cfg.kuzu_dir))
        #: 프로젝션 lag 계측 — 예산 <2s의 관찰 수단 (ADR-020 §1)
        self._lag = LagAggregator()
        #: Prometheus 지표 손잡이 — LF_METRICS_PORT 옵트인 시에만 주입된다
        self._metrics = metrics

    @property
    def graph(self) -> RelGraph:
        return self._graph

    def project(self, envelope: dict[str, Any]) -> None:
        """봉투 하나를 그래프에 반영한다. 목록 밖 타입은 무시(전방 호환)."""
        handler = HANDLERS.get(envelope["type"])
        if handler is not None:
            handler(self._graph, envelope["world_id"], envelope)

    def _sources(self) -> tuple[tuple[str, str, str], ...]:
        """(스트림, filter 조각, durable) — 은퇴·부활은 LF_ACTOR에서 따로 온다."""
        cfg = self._cfg
        return (
            ("LF_REL", "relationship.>", cfg.kuzu_durable),
            ("LF_ACTOR", RETIRED_TYPE, f"{cfg.kuzu_durable}-retire"),
            ("LF_ACTOR", RETURNED_TYPE, f"{cfg.kuzu_durable}-return"),
        )

    async def reproject_returned(
        self, conn: AsyncConnection, envelope: dict[str, Any]
    ) -> int:
        """부활 재사영 — 그에 닿은 관계 역사(returned 이전)를 같은 project에 다시 먹인다.

        DETACH DELETE가 지운 노드+양방향 간선이 마지막 상태로 되살아난다
        (upsert 덮어쓰기 — 멱등). 라이브 소비와 from-es 리플레이가 이 함수 하나를
        부른다 (결정적 부활). 반환: 다시 먹인 관계 봉투 수.
        """
        fed = 0
        async for past in return_envelopes(conn, "kuzu", envelope):
            self.project(past)
            fed += 1
        return fed

    async def _returned(self, conn: AsyncConnection | None, envelope: dict[str, Any]) -> int:
        """conn이 없으면(라이브 소비) es가 사는 PG에 직접 접속한다 — from-es
        리빌드가 이미 같은 접근(cfg.database_url)을 쓰므로 계층 위반이 아니다."""
        if conn is not None:
            return await self.reproject_returned(conn, envelope)
        async with await AsyncConnection.connect(
            self._cfg.database_url, autocommit=True
        ) as own:
            return await self.reproject_returned(own, envelope)

    def replay_apply(
        self, conn: AsyncConnection | None = None
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """from-es 리플레이 어댑터 — NATS 대신 es 봉투를 같은 project에 먹인다.

        returned는 es 범위 재사영이라 conn이 필요하다 — 리플레이 호출자는
        읽고 있는 그 conn을 넘긴다 (같은 원천 = 결정적).
        """
        async def apply(envelope: dict[str, Any]) -> None:
            if envelope["type"] == RETURNED_TYPE:
                await self._returned(conn, envelope)
                return
            self.project(envelope)
        return apply

    async def _consume(
        self, nc: nats.NATS, stream: str, segment: str, durable: str,
        stop: asyncio.Event, once: bool,
    ) -> None:
        cfg = self._cfg
        js = nc.jetstream()
        filter_subject = f"lf.{cfg.env}.*.{segment}"
        psub = await js.pull_subscribe(filter_subject, durable=durable, stream=stream)
        logger.info(
            "kuzu-projector 대기 — filter=%s durable=%s dir=%s",
            filter_subject, durable, cfg.kuzu_dir,
        )
        async for msgs in batches(
            psub, batch_size=cfg.batch_size, timeout_s=cfg.fetch_timeout_s, stop=stop, once=once
        ):
            for msg in msgs:
                await self._handle(msg, js)
        # pull 구독을 남기면 nc.drain()이 타임아웃(30s)까지 매달린다 — 명시 해지
        await psub.unsubscribe()

    async def _consume_all(self, nc: nats.NATS, stop: asyncio.Event, once: bool) -> None:
        await asyncio.gather(
            *(
                self._consume(nc, stream, segment, durable, stop, once)
                for stream, segment, durable in self._sources()
            )
        )
        if once:
            stop.set()  # 두 소스가 모두 드레인되면 나란히 도는 graph API도 내린다

    async def _handle(self, msg: Any, js: Any) -> None:
        cfg = self._cfg
        try:
            envelope = json.loads(msg.data)
            if envelope["type"] == RETURNED_TYPE:
                fed = await self._returned(None, envelope)
                logger.info(
                    "부활 재사영 — actor=%s 관계 봉투 %d건",
                    envelope["payload"]["actor_id"], fed,
                )
            else:
                self.project(envelope)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            subj = dlq_subject(cfg.env, msg.subject)
            await js.publish(
                subj, msg.data,
                headers={"Nats-Msg-Id": f"dlq-kuzu-{msg.metadata.sequence.stream}"},
            )
            logger.warning("그래프 반영 불가 → DLQ %s: %s", subj, e)
            await msg.ack()
        except Exception:
            if msg.metadata.num_delivered >= cfg.max_deliver:
                subj = dlq_subject(cfg.env, msg.subject)
                await js.publish(
                    subj, msg.data,
                    headers={"Nats-Msg-Id": f"dlq-kuzu-{msg.metadata.sequence.stream}"},
                )
                logger.exception("반영 반복 실패 → DLQ %s", subj)
                await msg.ack()
            else:
                logger.exception("반영 일시 오류 — 재전달 예약")
                await msg.nak(delay=cfg.nak_delay_s)
        else:
            observe(self._lag, envelope, logger, metrics=self._metrics)
            await msg.ack()

    async def run(
        self, *, stop: asyncio.Event | None = None, rebuild: bool = False, once: bool = False
    ) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        nc = await nats.connect(cfg.nats_url)
        try:
            if rebuild:
                logger.info("재구축 — Kuzu %s 와 durable %s 파괴", cfg.kuzu_dir, cfg.kuzu_durable)
                self._graph.drop_all()
                for stream, _, durable in self._sources():
                    try:
                        await nc.jetstream().delete_consumer(stream, durable)
                    except NotFoundError:
                        pass
            # 소비 루프와 질의 API가 한 프로세스에서 나란히 돈다 (임베디드 중재)
            await asyncio.gather(
                self._consume_all(nc, stop, once),
                serve_graph_api(nc, self._graph, cfg.env, stop=stop),
            )
        finally:
            await nc.drain()
            self._graph.close()
