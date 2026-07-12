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

logger = logging.getLogger("lf.actor.mailbox")

SOURCE_STREAM = "LF_PLAYER"
DURABLE = "actor-mailbox"

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


async def run_mailbox_router(
    nc: nats.NATS,
    mailbox: Mailbox,
    env: str,
    *,
    stop: asyncio.Event | None = None,
    batch_size: int = 64,
    fetch_timeout_s: float = 5.0,
) -> None:
    """LF_PLAYER → 대상 액터 메일박스 라우팅 루프.

    멱등: 메일박스는 최대 1회 소비 큐가 아니라 지각 버퍼다 — 재전달로 같은
    개입이 두 번 들어가도 응답 이벤트의 causation dedup은 후속 과제이며,
    Phase 1에서는 ack 이전 crash 창이 충분히 좁다 (조용한 유실 금지가 우선).
    """
    stop = stop or asyncio.Event()
    js = nc.jetstream()
    # 지각 소스 2종: 플레이어 개입(대상 1명) + 세계 사건(영향권 전원, ADR-013)
    subs = [
        await js.pull_subscribe(
            f"lf.{env}.*.player.>", durable=DURABLE, stream=SOURCE_STREAM
        ),
        await js.pull_subscribe(
            f"lf.{env}.*.world.incident.occurred",
            durable=f"{DURABLE}-incident", stream="LF_WORLD",
        ),
    ]
    logger.info("메일박스 라우터 대기 — durable=%s (플레이어+세계사건)", DURABLE)

    def targets_of(envelope: dict[str, Any]) -> list[str]:
        if envelope["type"] == "world.incident.occurred":
            return list(envelope["payload"]["affected_actor_ids"])
        return [envelope["payload"]["target_actor_id"]]

    async def route(msg: Any) -> None:
        try:
            envelope = json.loads(msg.data)
            for target in targets_of(envelope):
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
