"""advisory 안전망 통합 검증 — 실제 NATS JetStream 대상 (conftest 참고).

DLQ 이동 로직이 없는(또는 이동 전에 크래시한) 소비자를 흉내 내 MAX_DELIVERIES
advisory를 유발하고, 안전망이 원본을 lf-dlq.> 로 재발행하는지 본다 (ADR-017 §4).
"""

import asyncio
import json

import nats.errors
from lf_dispatcher.advisory import republish_from_advisory, run_advisory_watcher
from lf_dispatcher.config import Config
from lf_dispatcher.subjects import dlq_subject
from nats.js.api import ConsumerConfig

from .conftest import NATS_URL, PG_DSN

ENV = "test"
SUBJECT = f"lf.{ENV}.w_test.system.tick.completed"
PAYLOAD = json.dumps({"tick": 1}).encode()


async def provoke_advisory(js) -> None:
    """max_deliver=1 소비자가 nak 후 재fetch — MAX_DELIVERIES advisory 유발.

    JetStream은 nak 자체가 아니라 다음 재전달 시도가 소진(deliveries >=
    max_deliver)을 발견할 때 advisory를 발행한다 — 재fetch가 그 시도를 만든다.
    """
    psub = await js.pull_subscribe(
        SUBJECT, durable="crashy", stream="LF_SYS",
        config=ConsumerConfig(max_deliver=1, ack_wait=1.0),
    )
    try:
        msgs = await psub.fetch(1, timeout=5)
        await msgs[0].nak()
        try:
            await psub.fetch(1, timeout=2)  # 소진된 메시지라 재전달 없이 timeout이 정상
        except (nats.errors.TimeoutError, TimeoutError):
            pass
    finally:
        await psub.unsubscribe()


async def drain_dlq(js, batch: int = 10) -> list:
    sub = await js.pull_subscribe("lf-dlq.>", stream="LF_DLQ")
    try:
        return await sub.fetch(batch, timeout=2)
    except (nats.errors.TimeoutError, TimeoutError):
        return []
    finally:
        await sub.unsubscribe()


async def test_advisory_republishes_original_to_dlq(js):
    await js.publish(SUBJECT, PAYLOAD)
    await provoke_advisory(js)

    # 안전망은 advisory 발생 뒤에 시작한다 — 스트림 캡처 덕에 다운타임 중 advisory도 잡는다
    cfg = Config(pg_dsn=PG_DSN, nats_url=NATS_URL, env=ENV)
    stop = asyncio.Event()
    task = asyncio.create_task(run_advisory_watcher(cfg, stop=stop))
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while (await js.stream_info("LF_DLQ")).state.messages < 1:
            assert asyncio.get_running_loop().time() < deadline, "DLQ 재발행 대기 시간 초과"
            await asyncio.sleep(0.2)
    finally:
        stop.set()
        await task

    msgs = await drain_dlq(js)
    assert [m.subject for m in msgs] == [dlq_subject(ENV, SUBJECT)]
    assert msgs[0].data == PAYLOAD  # 원본 payload 그대로 — 안전망은 내용을 건드리지 않는다


async def test_republish_is_deduplicated(js):
    ack = await js.publish(SUBJECT, PAYLOAD)
    advisory = {"stream": "LF_SYS", "stream_seq": ack.seq, "consumer": "crashy"}

    assert await republish_from_advisory(js, ENV, advisory)
    assert await republish_from_advisory(js, ENV, advisory)  # advisory 재전달 시나리오

    # Nats-Msg-Id dedup window가 중복을 흡수한다 (ADR-017 §1과 동일한 장치)
    info = await js.stream_info("LF_DLQ")
    assert info.state.messages == 1


async def test_missing_original_is_skipped(js):
    # 원본이 보존기한 만료 등으로 이미 사라진 경우 — 경고 후 전진, 세계는 멈추지 않는다
    ghost = {"stream": "LF_SYS", "stream_seq": 12_345, "consumer": "ghost"}
    assert not await republish_from_advisory(js, ENV, ghost)

    info = await js.stream_info("LF_DLQ")
    assert info.state.messages == 0


async def test_dlq_stream_advisory_is_not_republished(js):
    # DLQ 소비자(운영자 도구)의 max_deliver 소진은 재발행하지 않는다 — subject 중첩/루프 방지
    ack = await js.publish(dlq_subject(ENV, SUBJECT), PAYLOAD)
    advisory = {"stream": "LF_DLQ", "stream_seq": ack.seq, "consumer": "op-tool"}
    assert not await republish_from_advisory(js, ENV, advisory)

    info = await js.stream_info("LF_DLQ")
    assert info.state.messages == 1  # 이미 있던 1건 그대로
