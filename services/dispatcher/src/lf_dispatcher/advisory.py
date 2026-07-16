"""MAX_DELIVERIES advisory 안전망 — 놓친 DLQ 이동의 최종 방어선 (ADR-017 §4).

각 소비자는 max_deliver 소진 시 스스로 원본을 DLQ로 옮기지만, 그 로직이 없거나
이동 도중 크래시하면 메시지는 재전달이 끝난 채 조용히 잠든다. JetStream이
재전달을 포기할 때 발행하는 MAX_DELIVERIES advisory를 스트림(LF_ADVISORY)으로
붙잡아 durable로 소비하고, 원본을 조회해 DLQ로 재발행하는 플랫폼 안전망이다.

여러 인스턴스(relay 리더 + standby)가 함께 돌아도 안전하다 — durable이 작업을
분배하고, Nats-Msg-Id(dlq-advisory-<stream>-<stream_seq>)가 중복 재발행을 흡수한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from lf_dispatcher.config import Config
from lf_dispatcher.streams import ADVISORY_STREAM, MAX_DELIVERIES_ADVISORY, ensure_streams
from lf_dispatcher.subjects import dlq_subject

logger = logging.getLogger("lf.dispatcher.advisory")

WATCHER_DURABLE = "dispatcher-advisory-watcher"

#: 재발행 제외 스트림 — DLQ 재진입은 subject 중첩을 만들고(운영자 도구가 max_deliver를
#: 소진하는 경우), advisory 스트림 자신에 대한 advisory는 안전망의 재귀다
SKIP_STREAMS = frozenset({"LF_DLQ", ADVISORY_STREAM})

BATCH_SIZE = 64
FETCH_TIMEOUT_S = 2.0


async def republish_from_advisory(js: Any, env: str, advisory: dict[str, Any]) -> bool:
    """advisory가 가리키는 원본을 DLQ로 재발행한다. 반환: 발행했는가.

    원본 조회 실패(보존기한 만료로 이미 삭제 등)는 경고 후 전진한다 —
    조용한 유실 금지가 목표지만, 안전망이 세계를 멈춰서도 안 된다.
    """
    stream, seq = advisory["stream"], advisory["stream_seq"]
    if stream in SKIP_STREAMS:
        return False
    try:
        raw = await js.get_msg(stream, seq)
    except Exception as e:
        logger.warning(
            "원본 조회 실패 — 전진: stream=%s seq=%s consumer=%s 사유=%r",
            stream, seq, advisory.get("consumer"), e,
        )
        return False
    dlq = dlq_subject(env, raw.subject)
    # 알림 대상 로그 — DLQ 적재는 Runbook 항목이다 (ADR-017 §4)
    await js.publish(dlq, raw.data or b"", headers={"Nats-Msg-Id": f"dlq-advisory-{stream}-{seq}"})
    logger.warning(
        "max_deliver 소진 → DLQ 재발행: subject=%s stream=%s seq=%s consumer=%s",
        dlq, stream, seq, advisory.get("consumer"),
    )
    return True


async def _handle(msg: Any, js: Any, env: str) -> None:
    try:
        await republish_from_advisory(js, env, json.loads(msg.data))
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        # 형식이 깨진 advisory — 재시도해도 같은 결과다, 경고 후 전진
        logger.warning("advisory 해석 불가 — 전진: subject=%s 사유=%r", msg.subject, e)
    # DLQ 발행 실패는 ack 전에 전파된다 — 미ack advisory는 재전달로 다시 온다
    await msg.ack()


async def run_advisory_watcher(cfg: Config, *, stop: asyncio.Event | None = None) -> None:
    """advisory 안전망 메인 루프. stop 이벤트가 셋되면 정상 종료한다."""
    import nats

    stop = stop or asyncio.Event()
    nc = await nats.connect(cfg.nats_url)
    try:
        js = nc.jetstream()
        # relay는 리더만 ensure_streams를 부르므로 standby의 안전망은 스스로 보장 (멱등)
        await ensure_streams(js)
        psub = await js.pull_subscribe(
            MAX_DELIVERIES_ADVISORY, durable=WATCHER_DURABLE, stream=ADVISORY_STREAM
        )
        logger.info("advisory 안전망 대기 — durable=%s env=%s", WATCHER_DURABLE, cfg.env)
        try:
            while not stop.is_set():
                try:
                    msgs = await psub.fetch(BATCH_SIZE, timeout=FETCH_TIMEOUT_S)
                except (TimeoutError, nats.errors.TimeoutError):
                    continue  # 유휴 타임아웃은 정상 흐름이다 (composer와 동일한 이유)
                for msg in msgs:
                    await _handle(msg, js, cfg.env)
        finally:
            # fetch 잔여물이 남은 pull 구독은 drain을 붙잡는다 — 먼저 정리한다
            await psub.unsubscribe()
    finally:
        await nc.drain()
