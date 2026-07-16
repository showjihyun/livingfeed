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
from pathlib import Path
from typing import Any

import nats
import nats.errors
from lf_dispatcher.subjects import dlq_subject
from nats.js.errors import NotFoundError

from lf_projector.config import Config
from lf_projector.graph import RelGraph
from lf_projector.graph_api import serve_graph_api
from lf_projector.lag import LagAggregator, observe

logger = logging.getLogger("lf.projector.kuzu")

HANDLERS = {
    "relationship.state.changed": RelGraph.apply_state_changed,
    "relationship.milestone.reached": RelGraph.apply_milestone,
}


class KuzuProjector:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._graph = RelGraph(Path(cfg.kuzu_dir))
        #: 프로젝션 lag 계측 — 예산 <2s의 관찰 수단 (ADR-020 §1)
        self._lag = LagAggregator()

    @property
    def graph(self) -> RelGraph:
        return self._graph

    def project(self, envelope: dict[str, Any]) -> None:
        """봉투 하나를 그래프에 반영한다. 모르는 relationship.* 타입은 무시(전방 호환)."""
        handler = HANDLERS.get(envelope["type"])
        if handler is not None:
            handler(self._graph, envelope["world_id"], envelope)

    async def _consume(self, nc: nats.NATS, stop: asyncio.Event) -> None:
        cfg = self._cfg
        js = nc.jetstream()
        filter_subject = f"lf.{cfg.env}.*.relationship.>"
        psub = await js.pull_subscribe(
            filter_subject, durable=cfg.kuzu_durable, stream="LF_REL"
        )
        logger.info(
            "kuzu-projector 대기 — filter=%s durable=%s dir=%s",
            filter_subject, cfg.kuzu_durable, cfg.kuzu_dir,
        )
        while not stop.is_set():
            try:
                msgs = await psub.fetch(cfg.batch_size, timeout=cfg.fetch_timeout_s)
            except (TimeoutError, nats.errors.TimeoutError):
                continue
            for msg in msgs:
                await self._handle(msg, js)

    async def _handle(self, msg: Any, js: Any) -> None:
        cfg = self._cfg
        try:
            envelope = json.loads(msg.data)
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
            observe(self._lag, envelope, logger)
            await msg.ack()

    async def run(self, *, stop: asyncio.Event | None = None, rebuild: bool = False) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        nc = await nats.connect(cfg.nats_url)
        try:
            if rebuild:
                logger.info("재구축 — Kuzu %s 와 durable %s 파괴", cfg.kuzu_dir, cfg.kuzu_durable)
                self._graph.drop_all()
                try:
                    await nc.jetstream().delete_consumer("LF_REL", cfg.kuzu_durable)
                except NotFoundError:
                    pass
            # 소비 루프와 질의 API가 한 프로세스에서 나란히 돈다 (임베디드 중재)
            await asyncio.gather(
                self._consume(nc, stop),
                serve_graph_api(nc, self._graph, cfg.env, stop=stop),
            )
        finally:
            await nc.drain()
            self._graph.close()
