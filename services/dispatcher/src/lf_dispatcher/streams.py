"""JetStream 스트림 프로비저닝 (ADR-004 §Stream 구성).

dispatcher가 시작 시 멱등하게 보장한다. 스트림 정의 변경은 ADR-004 개정 사항.
LF_PLAYER/LF_DLQ/LF_ADVISORY는 ADR-004 표의 보완이다 — envelope의 player 스트림,
DLQ subject(lf-dlq.>, ADR-017 §4), advisory 안전망 소스가 각각 필요하다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from nats.js import JetStreamContext
from nats.js.api import RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError

#: 발행 멱등성 창 — relay 재시도 중복을 Nats-Msg-Id로 흡수한다 (ADR-017 §1)
DUPLICATE_WINDOW = timedelta(minutes=2)

#: 소비자가 max_deliver를 소진하면 JetStream이 발행하는 advisory subject (ADR-017 §4)
MAX_DELIVERIES_ADVISORY = "$JS.EVENT.ADVISORY.CONSUMER.MAX_DELIVERIES.>"

#: advisory 안전망(advisory.py)이 소비하는 스트림 이름
ADVISORY_STREAM = "LF_ADVISORY"

_DAY = 86_400


@dataclass(frozen=True)
class StreamSpec:
    name: str
    subjects: tuple[str, ...]
    max_age_s: int
    max_bytes: int = -1


STREAMS: tuple[StreamSpec, ...] = (
    StreamSpec("LF_ACTOR", ("lf.*.*.actor.>",), 7 * _DAY, 50 * 1024**3),
    StreamSpec("LF_WORLD", ("lf.*.*.world.>",), 7 * _DAY),
    StreamSpec("LF_REL", ("lf.*.*.relationship.>",), 7 * _DAY),
    StreamSpec("LF_FEED", ("lf.*.*.feed.>",), 7 * _DAY),
    StreamSpec("LF_PLAYER", ("lf.*.*.player.>",), 7 * _DAY),
    StreamSpec("LF_SYS", ("lf.*.*.system.>",), 2 * _DAY),
    # DLQ: 운영자 도구가 소비 — 재처리/폐기 판단 시간을 벌기 위해 14일 (ADR-017 §4)
    # 프리픽스가 lf-dlq 인 이유는 subjects.dlq_subject docstring 참고 (패턴 중첩 금지)
    StreamSpec("LF_DLQ", ("lf-dlq.>",), 14 * _DAY),
    # advisory 안전망의 소스 — 코어 구독은 dispatcher가 내려간 동안의 advisory를
    # 놓친다. 스트림으로 붙잡아야 안전망 자신도 at-least-once다 (ADR-017 §4, advisory.py)
    StreamSpec(ADVISORY_STREAM, (MAX_DELIVERIES_ADVISORY,), 2 * _DAY),
)


def _config(spec: StreamSpec) -> StreamConfig:
    return StreamConfig(
        name=spec.name,
        subjects=list(spec.subjects),
        retention=RetentionPolicy.LIMITS,
        max_age=float(spec.max_age_s),
        max_bytes=spec.max_bytes,
        duplicate_window=DUPLICATE_WINDOW.total_seconds(),
    )


async def ensure_streams(js: JetStreamContext) -> None:
    """정의된 스트림을 생성하거나 정의에 맞게 갱신한다 (멱등)."""
    for spec in STREAMS:
        config = _config(spec)
        try:
            await js.stream_info(spec.name)
        except NotFoundError:
            await js.add_stream(config)
        else:
            await js.update_stream(config)
