"""redis-projector — LF_REL/LF_FEED/LF_ACTOR → Redis 타임라인 (ADR-003, ADR-014).

프로젝터 계약 준수:
1. 멱등 — ZADD(같은 member 재기록)·SADD 자연 멱등
2. 체크포인트 — 스트림별 JetStream durable consumer
3. 재구축 — --rebuild 가 타임라인 키와 durable을 파괴 후 처음부터 재소비
4. 단방향 — 도메인 이벤트를 발행하지 않는다
5. 격리 — 다른 프로젝터와 consumer 독립

소스 여섯이 곧 팬아웃 파이프라인이다:
- relationship.*           → 팔로워 인덱스 stand-in + 변화 리시트 (액터→플레이어 Private)
- player.follow.changed    → 명시 팔로우/철회 (진짜 팔로우 모델 — 철회가 이긴다)
- feed.post.published      → 팔로워 타임라인으로 fan-out-on-write
- actor.message.sent       → 수신 플레이어 타임라인 (Private 단독 배달)
- actor.identity.retired   → 은퇴 소멸 — 팔로워 인덱스와 타임라인의 발신분을 걷는다
- actor.identity.returned  → 부활 재사영 — es에서 그 액터 범위를 위 넷에 다시 먹인다
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import nats
from lf_dispatcher.subjects import dlq_subject
from nats.js.errors import NotFoundError
from psycopg import AsyncConnection
from redis.asyncio import Redis

from lf_projector.config import Config
from lf_projector.consume import batches
from lf_projector.lag import KindMetrics, LagAggregator, observe
from lf_projector.replay import matches, return_envelopes
from lf_projector.timeline import TimelineStore, follower_pair

logger = logging.getLogger("lf.projector.redis")

REPLY_TYPE = "actor.message.sent"
POST_TYPE = "feed.post.published"
FOLLOW_TYPE = "player.follow.changed"
#: 은퇴 소멸 — 스튜디오 삭제의 이벤트화. 타임라인의 그 액터 발신분을 걷는다
RETIRED_TYPE = "actor.identity.retired"
#: 부활 재사영 — 복원의 이벤트화. es에서 그 액터 범위를 기존 appliers에 다시 먹인다
RETURNED_TYPE = "actor.identity.returned"


class RedisProjector:
    def __init__(self, cfg: Config, metrics: KindMetrics | None = None) -> None:
        self._cfg = cfg
        #: 프로젝션 lag 계측 — 예산 <2s의 관찰 수단, 네 소스 공용 (ADR-020 §1)
        self._lag = LagAggregator()
        #: Prometheus 지표 손잡이 — LF_METRICS_PORT 옵트인 시에만 주입된다
        self._metrics = metrics

    def _sources(
        self, store: TimelineStore, conn: AsyncConnection | None = None
    ) -> tuple[tuple[str, str, str, Callable[[dict[str, Any]], Awaitable[None]]], ...]:
        """(스트림, durable, filter subject 조각, 핸들러).

        durable은 스트림 이름에서 파생되지만, LF_ACTOR를 여러 filter로 나눠 듣는
        은퇴·부활 소스만은 접미사(-retire/-return)로 갈라선다 — 같은 durable에
        다른 filter를 걸 수 없다 (consumer 독립, ADR-003 계약 5). conn은 부활
        재사영의 es 원천 — 리플레이가 넘기고, 라이브(None)는 직접 접속한다.
        """
        return (
            ("LF_REL", self._durable("LF_REL"), "relationship.>",
             self._apply_relationship(store)),
            ("LF_PLAYER", self._durable("LF_PLAYER"), FOLLOW_TYPE, self._apply_follow(store)),
            ("LF_FEED", self._durable("LF_FEED"), POST_TYPE, self._apply_post(store)),
            ("LF_ACTOR", self._durable("LF_ACTOR"), REPLY_TYPE, self._apply_reply(store)),
            ("LF_ACTOR", f"{self._cfg.redis_durable}-retire", RETIRED_TYPE,
             self._apply_retired(store)),
            ("LF_ACTOR", f"{self._cfg.redis_durable}-return", RETURNED_TYPE,
             self._apply_returned(store, conn)),
        )

    def _apply_relationship(self, store: TimelineStore):
        async def apply(envelope: dict[str, Any]) -> None:
            pair = follower_pair(envelope["payload"])
            if pair is not None:
                await store.register_follower(envelope["world_id"], *pair)
            # 변화 리시트 — 액터→플레이어 마음의 변화를 Private로 가시화 (도파민 §붕괴 방어)
            await store.push_receipt(envelope)
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

    def _apply_retired(self, store: TimelineStore):
        async def apply(envelope: dict[str, Any]) -> None:
            actor_id = envelope["payload"]["actor_id"]
            removed = await store.retire_actor(envelope["world_id"], actor_id)
            logger.info("은퇴 소멸 — actor=%s 타임라인 엔트리 %d건", actor_id, removed)
        return apply

    def _apply_returned(self, store: TimelineStore, conn: AsyncConnection | None = None):
        async def apply(envelope: dict[str, Any]) -> None:
            if conn is not None:
                fed = await self.reproject_returned(store, conn, envelope)
            else:
                # 라이브 소비 — es가 사는 PG에 직접 접속한다. from-es 리빌드가
                # 이미 같은 접근(cfg.database_url)을 쓰므로 계층 위반이 아니다.
                async with await AsyncConnection.connect(
                    self._cfg.database_url, autocommit=True
                ) as own:
                    fed = await self.reproject_returned(store, own, envelope)
            logger.info(
                "부활 재사영 — actor=%s 봉투 %d건 재적용",
                envelope["payload"]["actor_id"], fed,
            )
        return apply

    async def reproject_returned(
        self, store: TimelineStore, conn: AsyncConnection, envelope: dict[str, Any]
    ) -> int:
        """부활 재사영 — 그 액터 범위의 es(returned 이전)를 기존 appliers에 다시 먹인다.

        global_seq 순서라 팔로워 인덱스·거부 마커가 먼저 복원되고 그 위로 포스트
        팬아웃·답장·리시트가 다시 실린다 — retire_actor가 걷은 것과 대칭. ZADD/SADD
        재기록이라 재적용은 무연산(멱등)이고, 라이브와 from-es가 같은 경로다.
        범위 술어가 라이프사이클 이벤트를 제외하므로 재귀하지 않는다.
        """
        apply = self.replay_apply(store, conn)
        fed = 0
        async for past in return_envelopes(conn, "redis", envelope):
            await apply(past)
            fed += 1
        return fed

    def replay_apply(
        self, store: TimelineStore, conn: AsyncConnection | None = None
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """from-es 리플레이 어댑터 — _sources와 같은 술어로 적용자를 고른다.

        returned는 es 범위 재사영이라 conn이 필요하다 — 리플레이 호출자는
        읽고 있는 그 conn을 넘긴다 (같은 원천 = 결정적).
        """
        routes = tuple(
            (segment, apply) for _, _, segment, apply in self._sources(store, conn)
        )

        async def apply(envelope: dict[str, Any]) -> None:
            for pattern, fn in routes:
                if matches(pattern, envelope["type"]):
                    await fn(envelope)
                    return

        return apply

    def _durable(self, stream: str) -> str:
        return f"{self._cfg.redis_durable}-{stream.removeprefix('LF_').lower()}"

    async def _consume(
        self,
        nc: nats.NATS,
        stream: str,
        durable: str,
        segment: str,
        apply: Callable[[dict[str, Any]], Awaitable[None]],
        stop: asyncio.Event,
        once: bool,
    ) -> None:
        cfg = self._cfg
        js = nc.jetstream()
        filter_subject = f"lf.{cfg.env}.*.{segment}"
        psub = await js.pull_subscribe(filter_subject, durable=durable, stream=stream)
        logger.info(
            "redis-projector 대기 — filter=%s durable=%s", filter_subject, durable
        )
        async for msgs in batches(
            psub, batch_size=cfg.batch_size, timeout_s=cfg.fetch_timeout_s, stop=stop, once=once
        ):
            for msg in msgs:
                await self._handle(msg, apply, js)
        # pull 구독을 남기면 nc.drain()이 타임아웃(30s)까지 매달린다 — 명시 해지
        await psub.unsubscribe()

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
            observe(self._lag, envelope, logger, metrics=self._metrics)
            await msg.ack()

    async def _to_dlq(self, msg: Any, js: Any, *, reason: str) -> None:
        subj = dlq_subject(self._cfg.env, msg.subject)
        await js.publish(
            subj, msg.data,
            headers={"Nats-Msg-Id": f"dlq-redis-projector-{msg.metadata.sequence.stream}"},
        )
        logger.warning("DLQ 이동 — subject=%s 사유=%s", subj, reason)
        await msg.ack()

    async def run(
        self, *, stop: asyncio.Event | None = None, rebuild: bool = False, once: bool = False
    ) -> None:
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
                for stream, durable, _, _ in sources:
                    try:
                        await nc.jetstream().delete_consumer(stream, durable)
                    except NotFoundError:
                        pass
            await asyncio.gather(
                *(
                    self._consume(nc, stream, durable, segment, apply, stop, once)
                    for stream, durable, segment, apply in sources
                )
            )
        finally:
            await nc.drain()
            await redis.aclose()
