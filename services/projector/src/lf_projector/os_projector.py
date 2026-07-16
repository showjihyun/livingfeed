"""os-projector — LF_FEED → OpenSearch 색인 (ADR-003, ADR-014 §2단).

프로젝터 계약 준수:
1. 멱등 — _id=event_id upsert (자연 멱등 연산)
2. 체크포인트 — JetStream durable consumer
3. 재구축 — --rebuild 가 인덱스와 durable을 파괴 후 처음부터 재소비
4. 단방향 — 도메인 이벤트를 절대 발행하지 않는다 (DLQ 이동은 인프라 경로, ADR-017 §4)
5. 격리 — 다른 프로젝터와 consumer 독립
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

from lf_projector.config import Config
from lf_projector.lag import LagAggregator, observe
from lf_projector.os_index import OpenSearchIndex, envelope_to_doc

logger = logging.getLogger("lf.projector.os")

FEED_POST_TYPE = "feed.post.published"


class OsProjector:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        #: 프로젝션 lag 계측 — 예산 <2s의 관찰 수단 (ADR-020 §1)
        self._lag = LagAggregator()

    async def project_batch(
        self, msgs: list[Any], index: OpenSearchIndex, js: Any
    ) -> int:
        """배치를 색인하고 성공분만 ack한다. 반환: 색인 문서 수.

        파싱 불가 봉투(독약)는 배치에서 제외해 DLQ로 보낸다 — 색인 실패(일시
        오류)는 배치 전체를 nak해 재전달받는다 (upsert 멱등이라 안전하다).
        """
        docs: list[dict[str, Any]] = []
        good: list[Any] = []
        envelopes: list[dict[str, Any]] = []
        for msg in msgs:
            try:
                envelope = json.loads(msg.data)
                docs.append(envelope_to_doc(envelope))
                good.append(msg)
                envelopes.append(envelope)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                if msg.metadata.num_delivered >= self._cfg.max_deliver:
                    await self._to_dlq(msg, js, reason=repr(e))
                else:
                    await msg.nak(delay=self._cfg.nak_delay_s)

        if not docs:
            return 0
        try:
            await index.bulk_upsert(docs)
        except Exception:
            logger.exception("색인 실패 — 배치 %d건 재전달 예약", len(good))
            for msg in good:
                if msg.metadata.num_delivered >= self._cfg.max_deliver:
                    await self._to_dlq(msg, js, reason="색인 반복 실패")
                else:
                    await msg.nak(delay=self._cfg.nak_delay_s)
            return 0
        for msg, envelope in zip(good, envelopes, strict=True):
            observe(self._lag, envelope, logger)
            await msg.ack()
        return len(docs)

    async def _to_dlq(self, msg: Any, js: Any, *, reason: str) -> None:
        # 조용한 유실 금지 (ADR-017 §4). 도메인 발행이 아니라 인프라 이동이다 —
        # 프로젝터의 발행 금지 계약(ADR-003 계약 4)은 outbox/도메인 스트림에 적용된다.
        subj = dlq_subject(self._cfg.env, msg.subject)
        await js.publish(
            subj, msg.data,
            headers={"Nats-Msg-Id": f"dlq-os-projector-{msg.metadata.sequence.stream}"},
        )
        logger.warning("DLQ 이동 — subject=%s 사유=%s", subj, reason)
        await msg.ack()

    async def run(self, *, stop: asyncio.Event | None = None, rebuild: bool = False) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        filter_subject = f"lf.{cfg.env}.*.{FEED_POST_TYPE}"

        index = OpenSearchIndex(cfg.opensearch_url, cfg.index)
        nc = await nats.connect(cfg.nats_url)
        try:
            js = nc.jetstream()
            if rebuild:
                logger.info("재구축 — 인덱스 %s 와 durable %s 파괴", cfg.index, cfg.durable)
                await index.drop()
                try:
                    await js.delete_consumer(cfg.stream, cfg.durable)
                except NotFoundError:
                    pass
            await index.ensure()

            psub = await js.pull_subscribe(
                filter_subject, durable=cfg.durable, stream=cfg.stream
            )
            logger.info(
                "os-projector 대기 — filter=%s durable=%s index=%s",
                filter_subject, cfg.durable, cfg.index,
            )
            while not stop.is_set():
                try:
                    msgs = await psub.fetch(cfg.batch_size, timeout=cfg.fetch_timeout_s)
                except (TimeoutError, nats.errors.TimeoutError):
                    # nats-py fetch는 경로에 따라 asyncio.TimeoutError도 던진다
                    continue
                projected = await self.project_batch(msgs, index, js)
                if projected:
                    logger.info("색인 %d건", projected)
        finally:
            await nc.drain()
            await index.close()
