"""액터 메일박스 — 플레이어 상호작용의 유입 경로 (ADR-012 §메일박스).

라우터가 JetStream LF_PLAYER를 durable 소비해 대상 액터의 Redis 리스트에 넣고,
tick PERCEIVE가 drain해 인지 루프에 태운다. '상호작용 우선'(ADR-012 규칙 2)은
Phase 1에서 '다음 tick에 반드시 응답'으로 구현된다 — 액터 내 직렬성은
tick 파이프라인이 이미 보장한다 (동시 진입 없음).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any

import nats
import nats.errors
from lf_dispatcher.subjects import dlq_subject
from redis.asyncio import Redis

from lf_actor.social import FEED_POST_TYPE, FeedFanout, comment_targets

logger = logging.getLogger("lf.actor.mailbox")

SOURCE_STREAM = "LF_PLAYER"
DURABLE = "actor-mailbox"

#: 액터 소셜 루프의 순환 대상 — 피드 포스트(이웃 배달)와 액터 댓글(작성자 배달)
ACTOR_MESSAGE_TYPE = "actor.message.sent"

#: 메일박스 보존 — 오래 잠든 액터의 미처리 개입은 이 창이 지나면 소멸한다
MAILBOX_TTL = timedelta(hours=24)
MAILBOX_CAP = 100

MAX_DELIVER = 5
NAK_DELAY_S = 5.0


class Mailbox:
    """액터당 순차 수신함 (Redis 리스트, 오래된 것부터)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(world_id: str, actor_id: str) -> str:
        return f"lf:mb:{world_id}:{actor_id}"

    async def push(self, world_id: str, actor_id: str, envelope: dict[str, Any]) -> None:
        key = self._key(world_id, actor_id)
        pipe = self._redis.pipeline()
        pipe.rpush(key, json.dumps(envelope, ensure_ascii=False))
        pipe.ltrim(key, -MAILBOX_CAP, -1)
        pipe.expire(key, MAILBOX_TTL)
        await pipe.execute()

    async def drain(self, world_id: str, actor_id: str) -> list[dict[str, Any]]:
        """수신함을 비우며 전부 가져온다 (도착 순서)."""
        key = self._key(world_id, actor_id)
        pipe = self._redis.pipeline()
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        raw, _ = await pipe.execute()
        return [json.loads(item) for item in raw]

    async def drain_many(
        self, world_id: str, actor_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """여러 액터의 수신함을 한 파이프라인으로 비운다 (drain의 배치판, ADR-012).

        perceive는 매 tick 전 액터를 도는데, 액터마다 왕복하면 비용이 액터 수에
        비례한 RTT다 (유휴 세계에선 대부분 빈 수신함을 하나씩 확인하는 낭비).
        lrange+delete 쌍을 전 액터에 대해 하나의 파이프라인으로 묶어 1왕복으로
        줄인다. redis-py 파이프라인은 기본 MULTI/EXEC 트랜잭션이라 각 쌍의
        원자성(읽은 뒤 삭제)은 drain과 동일하게 보존된다. 항목 없는 액터는 빈 리스트.
        """
        if not actor_ids:
            return {}
        pipe = self._redis.pipeline()
        for actor_id in actor_ids:
            key = self._key(world_id, actor_id)
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
        results = await pipe.execute()
        # results는 [lrange_0, delete_0, lrange_1, delete_1, …] — lrange만 취한다
        return {
            actor_id: [json.loads(item) for item in results[i * 2]]
            for i, actor_id in enumerate(actor_ids)
        }


async def route_targets(
    envelope: dict[str, Any], fanout: FeedFanout | None
) -> list[str]:
    """봉투 하나의 메일박스 배달 대상 (라우터의 유일한 대상 판단 지점).

    피드 포스트는 관계 이웃 N명(FeedFanout), 액터 댓글은 글 작성자 1명
    (comment_targets — 깊이 1 차단), 세계 사건은 영향권 전원, 나머지
    (플레이어 개입·관측·승격·아크)는 payload의 대상 1명이다.
    """
    kind = envelope["type"]
    if kind == FEED_POST_TYPE:
        return await fanout.targets(envelope) if fanout is not None else []
    if kind == ACTOR_MESSAGE_TYPE:
        return comment_targets(envelope)
    if kind == "world.incident.occurred":
        return list(envelope["payload"]["affected_actor_ids"])
    return [envelope["payload"]["target_actor_id"]]


async def run_mailbox_router(
    nc: nats.NATS,
    mailbox: Mailbox,
    env: str,
    *,
    stop: asyncio.Event | None = None,
    batch_size: int = 64,
    fetch_timeout_s: float = 5.0,
    fanout: FeedFanout | None = None,
) -> None:
    """LF_PLAYER → 대상 액터 메일박스 라우팅 루프.

    멱등: 메일박스는 최대 1회 소비 큐가 아니라 지각 버퍼다 — 재전달로 같은
    개입이 두 번 들어가도 응답 이벤트의 causation dedup은 후속 과제이며,
    Phase 1에서는 ack 이전 crash 창이 충분히 좁다 (조용한 유실 금지가 우선).
    """
    stop = stop or asyncio.Event()
    js = nc.jetstream()
    # 지각/제어 소스 5종: 플레이어 개입(대상 1명) + 세계 사건(영향권 전원) +
    # Director의 사적 관측 nudge(대상 1명) + Director의 LOD 승격 spotlight(대상 1명) +
    # Director의 인생 아크 arc_planned(대상 1명, ADR-013). 관측·spotlight·아크는 공개
    # 사건이 아니라 target_actor_id 한 명에게만 간다. spotlight·아크는 지각이 아니라
    # 제어 신호다 — perceive가 LOD/ArcStore만 만지고 기억엔 안 남긴다.
    subs = [
        await js.pull_subscribe(
            f"lf.{env}.*.player.>", durable=DURABLE, stream=SOURCE_STREAM
        ),
        await js.pull_subscribe(
            f"lf.{env}.*.world.incident.occurred",
            durable=f"{DURABLE}-incident", stream="LF_WORLD",
        ),
        await js.pull_subscribe(
            f"lf.{env}.*.world.observation.surfaced",
            durable=f"{DURABLE}-observation", stream="LF_WORLD",
        ),
        await js.pull_subscribe(
            f"lf.{env}.*.system.director.spotlighted",
            durable=f"{DURABLE}-spotlight", stream="LF_SYS",
        ),
        await js.pull_subscribe(
            f"lf.{env}.*.system.director.arc_planned",
            durable=f"{DURABLE}-arc", stream="LF_SYS",
        ),
    ]
    if fanout is not None:
        # 액터 소셜 루프 (fanout 주입 시에만) — 피드 순환 + 액터 댓글 배달.
        # 라우터는 feed.post.published만 순환시킨다 — 댓글 자체는 순환하지 않고
        # 글 작성자 1명에게만 간다 (comment_targets, 깊이 1 차단).
        subs += [
            await js.pull_subscribe(
                f"lf.{env}.*.feed.post.published",
                durable=f"{DURABLE}-feed", stream="LF_FEED",
            ),
            await js.pull_subscribe(
                f"lf.{env}.*.actor.message.sent",
                durable=f"{DURABLE}-comment", stream="LF_ACTOR",
            ),
        ]
    logger.info(
        "메일박스 라우터 대기 — durable=%s (플레이어+세계사건+관측+승격+아크%s)",
        DURABLE, "+피드순환+댓글" if fanout is not None else "",
    )

    async def route(msg: Any) -> None:
        try:
            envelope = json.loads(msg.data)
            for target in await route_targets(envelope, fanout):
                await mailbox.push(envelope["world_id"], target, envelope)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            # 라우팅 불가 독약 — DLQ로 (조용한 유실 금지, ADR-017 §4)
            subj = dlq_subject(env, msg.subject)
            await js.publish(
                subj, msg.data,
                headers={"Nats-Msg-Id": f"dlq-mailbox-{msg.metadata.sequence.stream}"},
            )
            logger.warning("메일박스 라우팅 실패 → DLQ %s: %s", subj, e)
            await msg.ack()
        except Exception:
            if msg.metadata.num_delivered >= MAX_DELIVER:
                subj = dlq_subject(env, msg.subject)
                await js.publish(
                    subj, msg.data,
                    headers={"Nats-Msg-Id": f"dlq-mailbox-{msg.metadata.sequence.stream}"},
                )
                logger.exception("라우팅 반복 실패 → DLQ %s", subj)
                await msg.ack()
            else:
                logger.exception("라우팅 일시 오류 — 재전달 예약")
                await msg.nak(delay=NAK_DELAY_S)
        else:
            await msg.ack()

    while not stop.is_set():
        for psub in subs:
            try:
                msgs = await psub.fetch(batch_size, timeout=fetch_timeout_s)
            except (TimeoutError, nats.errors.TimeoutError):
                continue
            for msg in msgs:
                await route(msg)
