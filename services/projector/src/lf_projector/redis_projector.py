"""redis-projector — LF_REL/LF_FEED/LF_ACTOR → Redis 타임라인 (ADR-003, ADR-014).

프로젝터 계약 준수:
1. 멱등 — ZADD(같은 member 재기록)·SADD 자연 멱등
2. 체크포인트 — 스트림별 JetStream durable consumer
3. 재구축 — --rebuild 가 타임라인 키와 durable을 파괴 후 처음부터 재소비
4. 단방향 — 도메인 이벤트를 발행하지 않는다
5. 격리 — 다른 프로젝터와 consumer 독립

소스 넷이 곧 팬아웃 파이프라인이다:
- relationship.*        → 팔로워 인덱스 stand-in (누가 누구의 소식을 받는가)
- player.follow.changed → 명시 팔로우/철회 (진짜 팔로우 모델 — 철회가 이긴다)
- feed.post.published   → 팔로워 타임라인으로 fan-out-on-write
- actor.message.sent    → 수신 플레이어 타임라인 (Private 단독 배달)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import nats
import nats.errors
from lf_dispatcher.subjects import dlq_subject
from nats.js.errors import NotFoundError
from redis.asyncio import Redis

from lf_projector.config import Config
from lf_projector.lag import LagAggregator, observe
from lf_projector.timeline import TimelineStore, follower_pair

logger = logging.getLogger("lf.projector.redis")

REPLY_TYPE = "actor.message.sent"
POST_TYPE = "feed.post.published"
FOLLOW_TYPE = "player.follow.changed"


class RedisProjector:
    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        #: 프로젝션 lag 계측 — 예산 <2s의 관찰 수단, 네 소스 공용 (ADR-020 §1)
        self._lag = LagAggregator()

    def _sources(
        self, store: TimelineStore
    ) -> tuple[tuple[str, str, Callable[[dict[str, Any]], Awaitable[None]]], ...]:
        """(스트림, filter subject 조각, 핸들러) — durable은 스트림 이름에서 파생된다."""
        return (
            ("LF_REL", "relationship.>", self._apply_relationship(store)),
            ("LF_PLAYER", FOLLOW_TYPE, self._apply_follow(store)),
            ("LF_FEED", POST_TYPE, self._apply_post(store)),
            ("LF_ACTOR", REPLY_TYPE, self._apply_reply(store)),
        )

    def _apply_relationship(self, store: TimelineStore):
        async def apply(envelope: dict[str, Any]) -> None:
            pair = follower_pair(envelope["payload"])
            if pair is not None:
                await store.register_follower(envelope["world_id"], *pair)
        return apply

    def _apply_follow(self, store: TimelineStore):
        async def apply(envelope: dict[str, Any]) -> None:
            p = envelope["payload"]
            await store.set_follow(
                envelope["world_id"], p["target_actor_id"],
                p["player_id"], bool(p["following"]),
            )
            logger.info(
                "팔로우 %s — %s → %s",
                "선언" if p["following"] else "철회", p["player_id"], p["target_actor_id"],
            )
        return apply

    def _apply_post(self, store: TimelineStore):
        async def apply(envelope: dict[str, Any]) -> None:
            count = await store.fan_out_post(envelope)
            if count:
                logger.info("팬아웃 — post=%s 타임라인 %d개", envelope["event_id"], count)
        return apply

    def _apply_reply(self, store: TimelineStore):
        async def apply(envelope: dict[str, Any]) -> None:
            await store.push_reply(envelope)
        return apply

    def _durable(self, stream: str) -> str:
        return f"{self._cfg.redis_durable}-{stream.removeprefix('LF_').lower()}"

    async def _consume(
        self,
        nc: nats.NATS,
        stream: str,
        segment: str,
        apply: Callable[[dict[str, Any]], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None:
        cfg = self._cfg
        js = nc.jetstream()
        filter_subject = f"lf.{cfg.env}.*.{segment}"
        psub = await js.pull_subscribe(
            filter_subject, durable=self._durable(stream), stream=stream
        )
        logger.info(
            "redis-projector 대기 — filter=%s durable=%s", filter_subject, self._durable(stream)
        )
        while not stop.is_set():
            try:
                msgs = await psub.fetch(cfg.batch_size, timeout=cfg.fetch_timeout_s)
            except (TimeoutError, nats.errors.TimeoutError):
                continue
            for msg in msgs:
                await self._handle(msg, apply, js)

    async def _handle(
        self, msg: Any, apply: Callable[[dict[str, Any]], Awaitable[None]], js: Any
    ) -> None:
        cfg = self._cfg
        try:
            envelope = json.loads(msg.data)
            await apply(envelope)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            await self._to_dlq(msg, js, reason=repr(e))
        except Exception:
            if msg.metadata.num_delivered >= cfg.max_deliver:
                await self._to_dlq(msg, js, reason="반영 반복 실패")
            else:
                logger.exception("반영 일시 오류 — 재전달 예약")
                await msg.nak(delay=cfg.nak_delay_s)
        else:
            observe(self._lag, envelope, logger)
            await msg.ack()

    async def _to_dlq(self, msg: Any, js: Any, *, reason: str) -> None:
        subj = dlq_subject(self._cfg.env, msg.subject)
        await js.publish(
            subj, msg.data,
            headers={"Nats-Msg-Id": f"dlq-redis-projector-{msg.metadata.sequence.stream}"},
        )
        logger.warning("DLQ 이동 — subject=%s 사유=%s", subj, reason)
        await msg.ack()

    async def run(self, *, stop: asyncio.Event | None = None, rebuild: bool = False) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg
        redis = Redis.from_url(cfg.redis_url)
        store = TimelineStore(redis)
        nc = await nats.connect(cfg.nats_url)
        try:
            sources = self._sources(store)
            if rebuild:
                logger.info("재구축 — 타임라인 키와 durable 파괴")
                await store.drop_all()
                for stream, _, _ in sources:
                    try:
                        await nc.jetstream().delete_consumer(stream, self._durable(stream))
                    except NotFoundError:
                        pass
            await asyncio.gather(
                *(
                    self._consume(nc, stream, segment, apply, stop)
                    for stream, segment, apply in sources
                )
            )
        finally:
            await nc.drain()
            await redis.aclose()
