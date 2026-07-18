"""actor tick 워커 엔트리포인트 — 액터가 실린 tick engine (ADR-011/012).

실행:
    uv run --package lf-actor python -m lf_actor.main
설정: lf_tick.config의 것들 + NATS_URL, LF_REDIS_URL, LF_PERSONAS_DIR.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

import nats
from lf_tick.config import TickConfig
from lf_tick.engine import run_tick_loop
from lf_tick.shard import shard_of
from redis.asyncio import Redis

from lf_actor.arc import ArcStore
from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.goal import GoalAdapter
from lf_actor.ledger import DecayLedger
from lf_actor.mailbox import Mailbox, run_mailbox_router
from lf_actor.memory import WorkingMemory
from lf_actor.outreach import OutreachLedger
from lf_actor.phases import ActorPhases
from lf_actor.reflection import BeliefLedger
from lf_actor.relationship import RelationshipAdapter
from lf_actor.reload import PersonaReloader, ReloadingPhases
from lf_actor.semantic import SemanticMemory
from lf_actor.shard_barrier import RedisShardBarrier
from lf_actor.social import FeedFanout

logger = logging.getLogger("lf.actor.main")


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    cfg = TickConfig.from_env()
    env = os.environ.get("LF_ENV", "dev")
    nats_url = os.environ.get("NATS_URL", "nats://localhost:4222")
    redis_url = os.environ.get("LF_REDIS_URL", "redis://localhost:6379/0")
    personas_dir = Path(os.environ.get("LF_PERSONAS_DIR", "agents/personas"))
    # LLM decide 응답 예산 — reasoning 모델은 더 오래 걸릴 수 있다 (tick 예산 안에서)
    ai_timeout_s = float(os.environ.get("LF_AI_TIMEOUT_S", "10"))

    # 핫 리로드 감지기 — 스튜디오가 파일(SoT)을 바꾸면 tick 경계에서 세계에 반영된다
    reloader = PersonaReloader(personas_dir)
    personas = reloader.load()
    logger.info("페르소나 %d명 로드: %s", len(personas), ", ".join(p.id for p in personas))

    # 샤드 분할 (ADR-012 Phase 2) — 샤드 수는 고정 상수, 워커는 샤드 집합을 소유한다.
    # 실행 집합만 샤드별이고 이름·유효 대상·팬아웃 명부는 세계 전체다 (phases 로스터)
    num_shards = int(os.environ.get("LF_NUM_SHARDS", "1"))
    own_shards = {
        int(s) for s in os.environ.get("LF_SHARDS", "").split(",") if s.strip()
    } or set(range(num_shards))
    shard_select = (
        (lambda p: shard_of(p.id, num_shards) in own_shards) if num_shards > 1 else None
    )
    if num_shards > 1:
        owned = [p.id for p in personas if shard_of(p.id, num_shards) in own_shards]
        logger.info(
            "샤드 모드: %s / %d — 실행 %d명 (%s)",
            sorted(own_shards), num_shards, len(owned), ", ".join(owned) or "없음",
        )

    qdrant_url = os.environ.get("LF_QDRANT_URL", "http://localhost:6333")

    nc = await nats.connect(nats_url)
    redis = Redis.from_url(redis_url)
    semantic = SemanticMemory(qdrant_url)
    try:
        mailbox = Mailbox(redis)
        relationship = RelationshipAdapter(redis)
        phases = ActorPhases(
            personas,
            ai=AiRuntimeClient(nc, env, timeout_s=ai_timeout_s),
            memory=WorkingMemory(redis),
            mailbox=mailbox,
            emotion=EmotionAdapter(redis),
            relationship=relationship,
            semantic=semantic,
            goal=GoalAdapter(redis),
            belief_ledger=BeliefLedger(redis),
            reflection_interval=int(os.environ.get("LF_REFLECT_INTERVAL", "30")),
            identity_redis=redis,
            arc=ArcStore(redis),
            decay_ledger=DecayLedger(redis),
            # 고강도 사건 승격 임계 — 세계 톤에 맞춰 조정 가능 (ADR-011 관심 신호)
            promote_intensity=float(os.environ.get("LF_PROMOTE_INTENSITY", "0.7")),
            # 선제 DM 빈도 장부 — 없으면 '기억됨' 경로 자체가 조용히 꺼진다 (plan/02)
            outreach=OutreachLedger(redis),
            shard_select=shard_select,
        )
        barrier = (
            RedisShardBarrier(
                redis, cfg.world_id, own=own_shards, num_shards=num_shards
            )
            if num_shards > 1
            else None
        )
        # 액터 소셜 루프 — 피드 포스트를 관계 이웃에게, 액터 댓글을 글 작성자에게
        # (배달 수는 params.yaml social.feed_fanout이 원천)
        fanout = FeedFanout(relationship, [p.id for p in personas])
        # 리로드는 tick 경계(schedule 직전)에서만 — 새 액터는 Hot 데뷔, 명부는
        # fanout까지 따라온다. tick 중간 변경 금지 (reload.py)
        reloading = ReloadingPhases(
            phases, reloader,
            on_reload=lambda ps: fanout.set_roster([p.id for p in ps]),
        )
        # tick 루프(리더 또는 샤드 팔로워)와 메일박스 라우터가 나란히 돈다 (ADR-012).
        # 라우터는 전 워커가 함께 돈다 — durable이 배달을 분배하고 Redis가 공유 표면이다
        await asyncio.gather(
            run_tick_loop(cfg, reloading, stop=stop, barrier=barrier),
            run_mailbox_router(nc, mailbox, env, stop=stop, fanout=fanout),
        )
    finally:
        await semantic.close()
        await redis.aclose()
        await nc.drain()


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())


if __name__ == "__main__":
    main()
