"""FeedComposer — 이벤트 스트림 소비 → 편집 → feed.post.published 적재 (ADR-014 §1단).

소비: JetStream LF_ACTOR durable pull consumer (전 세계 actor.action.performed).
발행: 직접 NATS 발행이 아니라 append() → outbox → relay 경유가 유일 경로다 (ADR-017 §1).
멱등: post_id가 원본 event_id에서 결정적으로 파생되므로(compose.derive_post_id)
재전달·재시작의 중복 승격은 스트림 CAS(ConcurrencyConflict)가 거부한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import nats
import nats.errors
from lf_dispatcher.subjects import dlq_subject
from lf_eventstore import ConcurrencyConflict, ValidationFailed, append
from psycopg import AsyncConnection

from lf_feed.compose import (
    PRINCIPAL,
    build_goal_post_event,
    build_incident_post_event,
    build_post_event,
    evaluate,
    evaluate_goal_achievement,
    evaluate_incident,
    load_actor_names,
)
from lf_feed.config import Config
from lf_feed.scoring import RarityTracker

logger = logging.getLogger("lf.feed.composer")

SOURCE_EVENT_TYPE = "actor.action.performed"
INCIDENT_EVENT_TYPE = "world.incident.occurred"
GOAL_ACHIEVED_TYPE = "actor.goal.achieved"


class FeedComposer:
    """편집 1단 워커. 인스턴스 여러 개여도 durable consumer가 작업을 분배한다."""

    def __init__(self, cfg: Config, *, actor_names: dict[str, str] | None = None) -> None:
        self._cfg = cfg
        self._names = (
            actor_names if actor_names is not None else load_actor_names(cfg.personas_dir)
        )
        self._rarity = RarityTracker(cfg.scoring.rarity_window)

    async def compose_once(self, conn: AsyncConnection, envelope: dict[str, Any]) -> str | None:
        """봉투 하나를 편집한다. 반환: 승격된 post_id, 임계 미달이면 None."""
        if envelope["type"] == INCIDENT_EVENT_TYPE:
            # Director boost 항이 실값(1.0)인 유일한 소스 (ADR-013/014)
            drama, score = evaluate_incident(envelope, self._rarity, self._cfg.scoring)
            event = build_incident_post_event(envelope, drama=drama, score=score)
        elif envelope["type"] == GOAL_ACHIEVED_TYPE:
            # 목표 완주 — 인물의 마디, 세계 뉴스로 승격 (ADR-012/014)
            drama, score = evaluate_goal_achievement(envelope, self._cfg.scoring)
            event = build_goal_post_event(
                envelope, drama=drama, score=score, actor_names=self._names
            )
        else:
            drama, score = evaluate(envelope, self._rarity, self._cfg.scoring)
            event = build_post_event(
                envelope, drama=drama, score=score, actor_names=self._names
            )
        if score < self._cfg.scoring.threshold:
            logger.debug(
                "임계 미달 — event_id=%s score=%.3f < %.3f",
                envelope["event_id"], score, self._cfg.scoring.threshold,
            )
            return None
        await append(conn, PRINCIPAL, [event], expected_head=0)
        logger.info(
            "FeedItem 승격 — post_id=%s source=%s score=%.3f",
            event.event_id, envelope["event_id"], score,
        )
        return event.event_id

    async def _handle(self, msg: Any, conn: AsyncConnection, js: Any) -> None:
        num_delivered = msg.metadata.num_delivered
        try:
            envelope = json.loads(msg.data)
            await self.compose_once(conn, envelope)
        except ConcurrencyConflict:
            # 재전달이 이미 승격된 원본을 다시 가져왔다 — 멱등 흡수
            logger.info("중복 승격 거부(이미 발행됨) — subject=%s", msg.subject)
            await msg.ack()
        except (json.JSONDecodeError, KeyError, TypeError, ValidationFailed) as e:
            # 재시도해도 같은 결과인 독약 메시지 — 조용한 유실 금지, DLQ로 (ADR-017 §4)
            await self._to_dlq(msg, js, reason=repr(e))
        except Exception:
            if num_delivered >= self._cfg.max_deliver:
                logger.exception(
                    "처리 실패 %d회 초과 — DLQ 이동: subject=%s", num_delivered, msg.subject
                )
                await self._to_dlq(msg, js, reason=f"max_deliver({num_delivered}) 초과")
            else:
                logger.exception("일시 오류 — %.0fs 후 재전달: subject=%s",
                                 self._cfg.nak_delay_s, msg.subject)
                await msg.nak(delay=self._cfg.nak_delay_s)
        else:
            await msg.ack()

    async def _to_dlq(self, msg: Any, js: Any, *, reason: str) -> None:
        subj = dlq_subject(self._cfg.env, msg.subject)
        await js.publish(
            subj, msg.data, headers={"Nats-Msg-Id": f"dlq-composer-{msg.metadata.sequence.stream}"}
        )
        logger.warning("DLQ 이동 — subject=%s 사유=%s", subj, reason)
        await msg.ack()

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        cfg = self._cfg

        async with await AsyncConnection.connect(cfg.pg_dsn, autocommit=True) as conn:
            nc = await nats.connect(cfg.nats_url)
            try:
                js = nc.jetstream()
                # 편집 소스 3종: 액터 행동·목표 완주(LF_ACTOR) + 세계 사건(LF_WORLD)
                subs = [
                    await js.pull_subscribe(
                        f"lf.{cfg.env}.*.{SOURCE_EVENT_TYPE}",
                        durable=cfg.durable, stream=cfg.source_stream,
                    ),
                    await js.pull_subscribe(
                        f"lf.{cfg.env}.*.{INCIDENT_EVENT_TYPE}",
                        durable=f"{cfg.durable}-world", stream="LF_WORLD",
                    ),
                    await js.pull_subscribe(
                        f"lf.{cfg.env}.*.{GOAL_ACHIEVED_TYPE}",
                        durable=f"{cfg.durable}-goal", stream=cfg.source_stream,
                    ),
                ]
                logger.info(
                    "feed composer 대기 — durable=%s threshold=%.2f (소스: 행동+세계사건+목표완주)",
                    cfg.durable, cfg.scoring.threshold,
                )
                while not stop.is_set():
                    for psub in subs:
                        try:
                            msgs = await psub.fetch(
                                cfg.batch_size, timeout=cfg.fetch_timeout_s
                            )
                        except (TimeoutError, nats.errors.TimeoutError):
                            # nats-py fetch는 경로에 따라 asyncio.TimeoutError(=내장
                            # TimeoutError)도 던진다 — 유휴 타임아웃은 정상 흐름이다
                            continue
                        for msg in msgs:
                            await self._handle(msg, conn, js)
            finally:
                await nc.drain()
