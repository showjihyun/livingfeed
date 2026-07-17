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
from redis.asyncio import Redis

from lf_actor.arc import ArcStore
from lf_actor.client import AiRuntimeClient
from lf_actor.emotion import EmotionAdapter
from lf_actor.goal import GoalAdapter
from lf_actor.ledger import DecayLedger
from lf_actor.mailbox import Mailbox, run_mailbox_router
from lf_actor.memory import WorkingMemory
from lf_actor.persona import load_personas
from lf_actor.phases import ActorPhases
from lf_actor.reflection import BeliefLedger
from lf_actor.relationship import RelationshipAdapter
from lf_actor.semantic import SemanticMemory
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

    personas = load_personas(personas_dir)
    logger.info("페르소나 %d명 로드: %s", len(personas), ", ".join(p.id for p in personas))

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
        )
        # 액터 소셜 루프 — 피드 포스트를 관계 이웃에게, 액터 댓글을 글 작성자에게
        # (배달 수는 params.yaml social.feed_fanout이 원천)
        fanout = FeedFanout(relationship, [p.id for p in personas])
        # tick 루프와 메일박스 라우터(LF_PLAYER → Redis)가 나란히 돈다 (ADR-012)
        await asyncio.gather(
            run_tick_loop(cfg, phases, stop=stop),
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
