"""pg-projector — LF_ACTOR/LF_PLAYER → PG read 테이블 (ADR-003, ADR-008 후속).

프로젝터 계약 준수:
1. 멱등 — event_id PK ON CONFLICT / 신념은 자리 upsert + ULID 순서 가드
2. 체크포인트 — 스트림별 JetStream durable consumer
3. 재구축 — --rebuild 가 read 테이블과 durable을 파괴 후 처음부터 재소비
4. 단방향 — 도메인 이벤트를 발행하지 않는다 (DLQ 이동은 인프라 경로)
5. 격리 — 다른 프로젝터와 consumer 독립

두 스트림(LF_ACTOR, LF_PLAYER)을 durable 두 개로 나란히 소비한다 —
대화 히스토리는 양방향(플레이어 발신 + 액터 응답)이 모여야 완성된다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import nats
import nats.errors
from lf_dispatcher.subjects import dlq_subject
from nats.js.errors import NotFoundError
from psycopg import AsyncConnection

from lf_projector.config import Config
from lf_projector.pg_read import ReadStore

logger = logging.getLogger("lf.projector.pg")

#: (스트림, subject 패턴 조각) — durable은 pg-projector-<이름> 으로 파생된다.
#: LF_SYS는 아크(system.director.arc_planned) 프로젝션용 — 그 밖의 system.*은
#: PROJECTIONS에 없어 무시된다 (전방 호환)
SOURCES: tuple[tuple[str, str], ...] = (
    ("LF_ACTOR", "actor"),
    ("LF_PLAYER", "player"),
    ("LF_SYS", "system"),
)


class PgProjector:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg

    def _durable(self, stream: str) -> str:
        return f"{self._cfg.pg_durable}-{stream.removeprefix('LF_').lower()}"

    async def _consume(
        self, nc: nats.NATS, store: ReadStore, stream: str, segment: str, stop: asyncio.Event
    ) -> None:
        cfg = self._cfg
        js = nc.jetstream()
        filter_subject = f"lf.{cfg.env}.*.{segment}.>"
        psub = await js.pull_subscribe(
            filter_subject, durable=self._durable(stream), stream=stream
        )
        logger.info(
            "pg-projector 대기 — filter=%s durable=%s", filter_subject, self._durable(stream)
        )
        while not stop.is_set():
            try:
                msgs = await psub.fetch(cfg.batch_size, timeout=cfg.fetch_timeout_s)
            except (TimeoutError, nats.errors.TimeoutError):
                continue
            for msg in msgs:
                await self._handle(msg, store, js)

    async def _handle(self, msg: Any, store: ReadStore, js: Any) -> None:
        cfg = self._cfg
        try:
            applied = await store.apply(json.loads(msg.data))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            # 독약 봉투 — 재전달로 나아질 수 없다 (ADR-017 §4)
            await self._to_dlq(msg, js, reason=repr(e))
        except Exception:
            if msg.metadata.num_delivered >= cfg.max_deliver:
                await self._to_dlq(msg, js, reason="반영 반복 실패")
            else:
                logger.exception("반영 일시 오류 — 재전달 예약")
                await msg.nak(delay=cfg.nak_delay_s)
        else:
            if applied:
                logger.debug("반영 — %s", msg.subject)
            await msg.ack()

    async def _to_dlq(self, msg: Any, js: Any, *, reason: str) -> None:
        subj = dlq_subject(self._cfg.env, msg.subject)
        await js.publish(
            subj, msg.data,
            headers={"Nats-Msg-Id": f"dlq-pg-projector-{msg.metadata.sequence.stream}"},
        )
        logger.warning("DLQ 이동 — subject=%s 사유=%s", subj, reason)
        await msg.ack()

    async def run(self, *, stop: asyncio.Event | None = None, rebuild: bool = False) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        conn = await AsyncConnection.connect(cfg.database_url, autocommit=True)
        store = ReadStore(conn)
        nc = await nats.connect(cfg.nats_url)
        try:
            if rebuild:
                logger.info("재구축 — read 테이블과 durable 파괴")
                await store.drop()
                for stream, _ in SOURCES:
                    try:
                        await nc.jetstream().delete_consumer(stream, self._durable(stream))
                    except NotFoundError:
                        pass
            await store.ensure()
            await asyncio.gather(
                *(self._consume(nc, store, stream, segment, stop) for stream, segment in SOURCES)
            )
        finally:
            await nc.drain()
            await conn.close()
