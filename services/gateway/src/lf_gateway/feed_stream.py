"""피드 스트림 구독 — NATS 구독 → 사용자별 필터 → 전송 중립 이벤트 (ADR-010).

SSE 핸들러와 롱폴링 폴백이 이 내부 구독 API를 공유한다 —
프로토콜 2종의 핸들러 이중화 비용을 여기서 흡수한다 (ADR-010 완화책).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import nats.errors
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

from lf_gateway.config import Config
from lf_gateway.cursor import resume_start_time

FEED_KINDS = frozenset({"world", "community", "relationship", "personal", "private", "hidden"})


@dataclass(frozen=True)
class FeedFilter:
    """연결 하나의 구독 조건 — gateway는 이것 외의 상태를 갖지 않는다."""

    world_id: str
    kinds: frozenset[str]
    cursor: str | None = None

    def accepts(self, envelope: dict[str, Any]) -> bool:
        if envelope.get("payload", {}).get("visibility") not in self.kinds:
            return False
        # BY_START_TIME 재개는 slack만큼 과거에서 시작한다 — 커서 이하는 이미 본 것
        if self.cursor is not None and envelope.get("event_id", "") <= self.cursor:
            return False
        return True


async def open_feed_subscription(js: JetStreamContext, cfg: Config, flt: FeedFilter):
    """연결당 임시(ephemeral) push consumer — ack 없음, 재개는 커서가 담당한다."""
    if flt.cursor is None:
        consumer = ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=AckPolicy.NONE)
    else:
        consumer = ConsumerConfig(
            deliver_policy=DeliverPolicy.BY_START_TIME,
            opt_start_time=resume_start_time(flt.cursor, cfg.resume_slack_s),
            ack_policy=AckPolicy.NONE,
        )
    subject = f"lf.{cfg.env}.{flt.world_id}.feed.>"
    return await js.subscribe(subject, stream=cfg.stream, config=consumer)


def parse_feed_event(data: bytes, flt: FeedFilter) -> tuple[str, str, str] | None:
    """수신 메시지 → (event_id, type, data 원문). 필터 불통과면 None."""
    envelope = json.loads(data)
    if not flt.accepts(envelope):
        return None
    return envelope["event_id"], envelope["type"], data.decode()


def sse_event(event_id: str, event_type: str, data: str) -> str:
    """SSE 프레임 — id는 피드 커서(ULID)와 동일하다 (ADR-010)."""
    return f"id: {event_id}\nevent: {event_type}\ndata: {data}\n\n"


async def sse_frames(js: JetStreamContext, cfg: Config, flt: FeedFilter) -> AsyncIterator[str]:
    """SSE 프레임 스트림 — 구독→필터→프레이밍의 전 과정 (HTTP 핸들러는 이걸 그대로 싣는다)."""
    sub = await open_feed_subscription(js, cfg, flt)
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                msg = await sub.next_msg(timeout=cfg.heartbeat_s)
            except nats.errors.TimeoutError:
                yield ": hb\n\n"  # 유휴 하트비트 — 프록시 idle 종료 방지
                continue
            parsed = parse_feed_event(msg.data, flt)
            if parsed is not None:
                yield sse_event(*parsed)
    finally:
        await sub.unsubscribe()
