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
from lf_eventstore import ConcurrencyConflict, ValidationFailed, append, read_stream
from psycopg import AsyncConnection

from lf_feed.compose import (
    PRINCIPAL,
    build_arc_post_event,
    build_debut_post_event,
    build_goal_post_event,
    build_incident_post_event,
    build_post_event,
    evaluate,
    evaluate_arc_transition,
    evaluate_debut,
    evaluate_goal_achievement,
    evaluate_incident,
    humanize_ids,
    load_actor_communities,
    load_actor_names,
)
from lf_feed.config import Config
from lf_feed.narrator import FeedNarrator
from lf_feed.scoring import RarityTracker, RepeatTracker

logger = logging.getLogger("lf.feed.composer")

SOURCE_EVENT_TYPE = "actor.action.performed"
INCIDENT_EVENT_TYPE = "world.incident.occurred"
GOAL_ACHIEVED_TYPE = "actor.goal.achieved"
ARC_EVENT_TYPE = "system.director.arc_planned"
BOOST_EVENT_TYPE = "system.director.feed_boosted"
DEBUT_EVENT_TYPE = "actor.identity.declared"


async def previous_arc_stage(
    conn: AsyncConnection, world_id: str, actor_id: str, before_event_id: str
) -> str | None:
    """이 계획 이전, 같은 인물의 마지막 아크 stage — 없으면 첫 아크다.

    이벤트 스토어의 arc 스트림이 원천이라 재시작·재전달에도 결정적이다
    (아크는 세계 하루 1건의 저빈도라 스트림 전체를 읽어도 가볍다).
    """
    stored = await read_stream(conn, world_id, "system", "arc")
    stages = [
        s.envelope["payload"]["stage"]
        for s in stored
        if s.envelope["payload"].get("target_actor_id") == actor_id
        and s.envelope["event_id"] < before_event_id
    ]
    return stages[-1] if stages else None


class FeedComposer:
    """편집 1단 워커. 인스턴스 여러 개여도 durable consumer가 작업을 분배한다."""

    def __init__(
        self,
        cfg: Config,
        *,
        actor_names: dict[str, str] | None = None,
        actor_communities: dict[str, str] | None = None,
        narrator: Any | None = None,
    ) -> None:
        self._cfg = cfg
        self._names = (
            actor_names if actor_names is not None else load_actor_names(cfg.personas_dir)
        )
        #: 액터 id → 커뮤니티 id — 포스트에 실어 커뮤니티 피드를 채운다 (ADR-014).
        #: 무소속은 매핑에서 빠져 community_id=None(월드 피드에만)이 된다.
        self._communities = (
            actor_communities
            if actor_communities is not None
            else load_actor_communities(cfg.personas_dir)
        )
        self._rarity = RarityTracker(cfg.scoring.rarity_window)
        self._repeat = RepeatTracker(
            penalty=cfg.scoring.repeat_penalty,
            window_ticks=cfg.scoring.repeat_window_ticks,
        )
        #: Director의 편집 조명 (boost_feed, ADR-013/014): (world, actor) → (boost, until).
        #: composer는 전 세계를 소비하므로 세계가 키에 들어간다. in-memory — 재시작 시
        #: 남은 조명이 꺼진다 (세계 열거 없이는 es 복원 불가, 조명은 60 tick짜리라 허용)
        self._boosts: dict[tuple[str, str], tuple[float, int]] = {}
        #: 고드라마 포스트의 본문 서사화 (ADR-018 narrate) — None이면 언제나 템플릿.
        #: run()이 cfg.narrate에 따라 배선하고, 테스트는 스텁을 주입한다
        self._narrator = narrator

    async def compose_once(self, conn: AsyncConnection, envelope: dict[str, Any]) -> str | None:
        """봉투 하나를 편집한다. 반환: 승격된 post_id, 임계 미달이면 None."""
        if envelope["type"] == BOOST_EVENT_TYPE:
            # 편집 조명 (boost_feed) — 포스트가 아니라 제어 신호다: 조명만 켠다
            payload = envelope["payload"]
            self._boosts[(envelope["world_id"], payload["target_actor_id"])] = (
                float(payload["boost"]), int(payload["until_tick"])
            )
            logger.info(
                "편집 조명: %s/%s boost=%.2f until_tick=%d",
                envelope["world_id"], payload["target_actor_id"],
                payload["boost"], payload["until_tick"],
            )
            return None
        if envelope["type"] == INCIDENT_EVENT_TYPE:
            # Director boost 항이 실값(1.0)인 유일한 소스 (ADR-013/014)
            drama, score = evaluate_incident(envelope, self._rarity, self._cfg.scoring)
            event = build_incident_post_event(envelope, drama=drama, score=score)
        elif envelope["type"] == ARC_EVENT_TYPE:
            # 장이 넘어가는 순간만 서사다 — 같은 stage 재계획은 방향 조정일 뿐 (plan/08)
            payload = envelope["payload"]
            previous = await previous_arc_stage(
                conn, envelope["world_id"], payload["target_actor_id"], envelope["event_id"]
            )
            if previous == payload["stage"]:
                return None
            drama, score = evaluate_arc_transition(self._cfg.scoring)
            event = build_arc_post_event(
                envelope, previous_stage=previous, drama=drama, score=score,
                actor_names=self._names,
                community_id=self._communities.get(payload["target_actor_id"]),
            )
        elif envelope["type"] == GOAL_ACHIEVED_TYPE:
            # 목표 완주 — 인물의 마디, 세계 뉴스로 승격 (ADR-012/014)
            drama, score = evaluate_goal_achievement(envelope, self._cfg.scoring)
            event = build_goal_post_event(
                envelope, drama=drama, score=score, actor_names=self._names,
                community_id=self._communities.get(envelope["actor_id"]),
            )
        elif envelope["type"] == DEBUT_EVENT_TYPE:
            # 데뷔 — 세계가 새 인물을 받아들이는 순간 (페르소나 스튜디오의 방생).
            # 정체성은 세계당 1회 선언이라 도배가 없다 (plan/03 저자성)
            drama, score = evaluate_debut(self._cfg.scoring)
            event = build_debut_post_event(
                envelope, drama=drama, score=score,
                community_id=self._communities.get(envelope["actor_id"]),
            )
        else:
            drama, score = evaluate(
                envelope, self._rarity, self._cfg.scoring,
                director_boost=self._active_boost(envelope),
            )
            # 연속 반복 감쇠 — 같은 인물의 같은 종류 연속 승격은 새 마디가 아니다
            # (confront처럼 드라마 항만으로 임계를 넘는 종류의 도배 차단, ADR-014)
            author = envelope["actor_id"]
            kind = envelope["payload"]["action_kind"]
            score *= self._repeat.penalty(author, kind, tick=envelope["tick"])
            self._repeat.observe(
                author, kind, tick=envelope["tick"],
                promoted=score >= self._cfg.scoring.threshold,
            )
            event = build_post_event(
                envelope, drama=drama, score=score, actor_names=self._names,
                community_id=self._communities.get(envelope["actor_id"]),
            )
        if score < self._cfg.scoring.threshold:
            logger.debug(
                "임계 미달 — event_id=%s score=%.3f < %.3f",
                envelope["event_id"], score, self._cfg.scoring.threshold,
            )
            return None
        # 고드라마 포스트는 본문을 서사로 다듬는다 (ADR-018 narrate) — 승격 판정은
        # 이미 끝났다: 실패해도 템플릿 본문이 그대로 간다 (조용한 강등)
        if (
            self._narrator is not None
            and event.payload["drama_score"] >= self._cfg.narrate_threshold
        ):
            narrated = await self._narrator.narrate(
                title=event.payload["title"],
                body=event.payload["body"],
                participants=[
                    self._names.get(p, p) for p in event.payload["participants"]
                ],
                world_id=envelope["world_id"],
                tick=envelope["tick"],
            )
            if narrated:
                # 서사도 정화를 지난다 — LLM이 재료의 식별자를 되뱉을 수 있다
                event.payload["body"] = humanize_ids(narrated, self._names)[:2000]
                event.payload["narration_kind"] = "llm"
        await append(conn, PRINCIPAL, [event], expected_head=0)
        logger.info(
            "FeedItem 승격 — post_id=%s source=%s score=%.3f",
            event.event_id, envelope["event_id"], score,
        )
        return event.event_id

    def _active_boost(self, envelope: dict[str, Any]) -> float:
        """이 행동에 걸린 편집 조명 (boost_feed) — 기간이 지났으면 끈다."""
        key = (envelope["world_id"], envelope.get("actor_id") or "")
        lit = self._boosts.get(key)
        if lit is None:
            return 0.0
        boost, until_tick = lit
        if envelope["tick"] >= until_tick:
            self._boosts.pop(key, None)  # 조명은 영원하지 않다
            return 0.0
        return boost

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
                # LLM 내레이션 배선 — 명시 주입(테스트)이 없고 켜져 있을 때만.
                # rule 프로바이더면 narrate 미지원이라 자동 템플릿 폴백된다.
                if self._narrator is None and cfg.narrate:
                    self._narrator = FeedNarrator(nc, cfg.env)
                # 편집 소스 5종: 액터 행동·목표 완주·데뷔(LF_ACTOR) + 세계 사건
                # (LF_WORLD) + 인생 아크 계획(LF_SYS — 장 전환분만 승격)
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
                    await js.pull_subscribe(
                        f"lf.{cfg.env}.*.{ARC_EVENT_TYPE}",
                        durable=f"{cfg.durable}-arc", stream="LF_SYS",
                    ),
                    await js.pull_subscribe(
                        f"lf.{cfg.env}.*.{BOOST_EVENT_TYPE}",
                        durable=f"{cfg.durable}-boost", stream="LF_SYS",
                    ),
                    await js.pull_subscribe(
                        f"lf.{cfg.env}.*.{DEBUT_EVENT_TYPE}",
                        durable=f"{cfg.durable}-debut", stream=cfg.source_stream,
                    ),
                ]
                logger.info(
                    "feed composer 대기 — durable=%s threshold=%.2f "
                    "(소스: 행동+세계사건+목표완주+아크전환+편집조명+데뷔)",
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
