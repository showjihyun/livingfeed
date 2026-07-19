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
from lf_dispatcher.subjects import dlq_subject
from nats.js.errors import NotFoundError
from psycopg import AsyncConnection

from lf_projector.config import Config
from lf_projector.consume import batches
from lf_projector.lag import KindMetrics, LagAggregator, observe
from lf_projector.os_index import OpenSearchIndex, envelope_to_doc
from lf_projector.replay import return_envelopes

logger = logging.getLogger("lf.projector.os")

FEED_POST_TYPE = "feed.post.published"
#: 은퇴 소멸 — 스튜디오 삭제의 이벤트화. 그 액터의 포스트 문서를 인덱스에서 지운다
RETIRED_TYPE = "actor.identity.retired"
#: 부활 재색인 — 복원의 이벤트화. 그 액터의 포스트를 es(SoT)에서 다시 읽어 색인한다
RETURNED_TYPE = "actor.identity.returned"


async def reindex_returned(
    index: OpenSearchIndex, conn: AsyncConnection, envelope: dict[str, Any]
) -> int:
    """부활 재색인 — 그 액터의 feed.post.published(returned 이전)를 es에서 다시 먹인다.

    같은 문서 변환(envelope_to_doc)과 _id=event_id upsert라 재실행이 안전하고
    (자연 멱등), delete_by_actor(은퇴 소멸)와 정확히 대칭이다. 라이브 소비와
    from-es 리플레이가 이 함수 하나를 부른다 (결정적 부활). 반환: 색인 문서 수.
    """
    docs: list[dict[str, Any]] = []
    total = 0
    async for past in return_envelopes(conn, "os", envelope):
        docs.append(envelope_to_doc(past))
        if len(docs) >= 500:
            await index.bulk_upsert(docs)
            total += len(docs)
            docs = []
    await index.bulk_upsert(docs)
    return total + len(docs)


class OsProjector:
    def __init__(self, cfg: Config, metrics: KindMetrics | None = None) -> None:
        self._cfg = cfg
        #: 프로젝션 lag 계측 — 예산 <2s의 관찰 수단 (ADR-020 §1)
        self._lag = LagAggregator()
        #: Prometheus 지표 손잡이 — LF_METRICS_PORT 옵트인 시에만 주입된다
        self._metrics = metrics

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
            observe(self._lag, envelope, logger, metrics=self._metrics)
            await msg.ack()
        return len(docs)

    async def retire_batch(
        self, msgs: list[Any], index: OpenSearchIndex, js: Any
    ) -> int:
        """은퇴 봉투 배치 — 액터별 delete_by_query 소멸. 반환: 집행한 은퇴 수.

        소멸은 자연 멱등(재실행 0건)이라 재전달이 안전하다. participants에만
        낀 남의 포스트는 남긴다 (retire_query — 남의 글은 남의 역사).
        """
        done = 0
        for msg in msgs:
            try:
                envelope = json.loads(msg.data)
                deleted = await index.delete_by_actor(
                    envelope["world_id"], envelope["payload"]["actor_id"]
                )
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                if msg.metadata.num_delivered >= self._cfg.max_deliver:
                    await self._to_dlq(msg, js, reason=repr(e))
                else:
                    await msg.nak(delay=self._cfg.nak_delay_s)
            except Exception:
                if msg.metadata.num_delivered >= self._cfg.max_deliver:
                    await self._to_dlq(msg, js, reason="소멸 반복 실패")
                else:
                    await msg.nak(delay=self._cfg.nak_delay_s)
            else:
                logger.info(
                    "은퇴 소멸 — actor=%s 문서 %d건",
                    envelope["payload"]["actor_id"], deleted,
                )
                observe(self._lag, envelope, logger, metrics=self._metrics)
                await msg.ack()
                done += 1
        return done

    async def return_batch(
        self, msgs: list[Any], index: OpenSearchIndex, js: Any
    ) -> int:
        """부활 봉투 배치 — 액터별 es 재색인(reindex_returned). 반환: 집행한 부활 수.

        es 접속은 cfg.database_url — from-es 리빌드가 이미 쓰는 같은 접근이다
        (파생 원천 접근, 계층 위반 아님). 부활은 드문 사건이라 건별 접속을 수용한다.
        """
        done = 0
        for msg in msgs:
            try:
                envelope = json.loads(msg.data)
                async with await AsyncConnection.connect(
                    self._cfg.database_url, autocommit=True
                ) as conn:
                    restored = await reindex_returned(index, conn, envelope)
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                if msg.metadata.num_delivered >= self._cfg.max_deliver:
                    await self._to_dlq(msg, js, reason=repr(e))
                else:
                    await msg.nak(delay=self._cfg.nak_delay_s)
            except Exception:
                if msg.metadata.num_delivered >= self._cfg.max_deliver:
                    await self._to_dlq(msg, js, reason="재색인 반복 실패")
                else:
                    await msg.nak(delay=self._cfg.nak_delay_s)
            else:
                logger.info(
                    "부활 재색인 — actor=%s 문서 %d건",
                    envelope["payload"]["actor_id"], restored,
                )
                observe(self._lag, envelope, logger, metrics=self._metrics)
                await msg.ack()
                done += 1
        return done

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

    def _sources(self) -> tuple[tuple[str, str, str, Any], ...]:
        """(스트림, filter 조각, durable, 배치 핸들러) — 은퇴·부활은 LF_ACTOR에서 따로 온다."""
        cfg = self._cfg
        return (
            (cfg.stream, FEED_POST_TYPE, cfg.durable, self.project_batch),
            ("LF_ACTOR", RETIRED_TYPE, f"{cfg.durable}-retire", self.retire_batch),
            ("LF_ACTOR", RETURNED_TYPE, f"{cfg.durable}-return", self.return_batch),
        )

    async def _consume(
        self, nc: nats.NATS, index: OpenSearchIndex, stream: str, segment: str,
        durable: str, handler: Any, stop: asyncio.Event, once: bool,
    ) -> None:
        cfg = self._cfg
        js = nc.jetstream()
        filter_subject = f"lf.{cfg.env}.*.{segment}"
        psub = await js.pull_subscribe(filter_subject, durable=durable, stream=stream)
        logger.info(
            "os-projector 대기 — filter=%s durable=%s index=%s",
            filter_subject, durable, cfg.index,
        )
        async for msgs in batches(
            psub, batch_size=cfg.batch_size, timeout_s=cfg.fetch_timeout_s,
            stop=stop, once=once,
        ):
            projected = await handler(msgs, index, js)
            if projected:
                logger.info("반영 %d건 — filter=%s", projected, filter_subject)
        # pull 구독을 남기면 nc.drain()이 타임아웃(30s)까지 매달린다 — 명시 해지
        await psub.unsubscribe()

    async def run(
        self, *, stop: asyncio.Event | None = None, rebuild: bool = False, once: bool = False
    ) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        index = OpenSearchIndex(cfg.opensearch_url, cfg.index)
        nc = await nats.connect(cfg.nats_url)
        try:
            js = nc.jetstream()
            sources = self._sources()
            if rebuild:
                logger.info("재구축 — 인덱스 %s 와 durable %s 파괴", cfg.index, cfg.durable)
                await index.drop()
                for stream, _, durable, _ in sources:
                    try:
                        await js.delete_consumer(stream, durable)
                    except NotFoundError:
                        pass
            await index.ensure()
            await asyncio.gather(
                *(
                    self._consume(nc, index, stream, segment, durable, handler, stop, once)
                    for stream, segment, durable, handler in sources
                )
            )
        finally:
            await nc.drain()
            await index.close()
