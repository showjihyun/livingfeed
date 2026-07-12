"""ULID 커서 유틸 — 피드 커서와 SSE Last-Event-ID는 같은 좌표계다 (ADR-010).

ULID 앞 10자(Crockford Base32)가 밀리초 타임스탬프 48비트다.
커서 재개는 이 타임스탬프로 JetStream BY_START_TIME 소비를 시작하고,
커서보다 작거나 같은 event_id를 걸러내는 2단으로 동작한다 — gateway는
어떤 소비 상태도 저장하지 않는다 (무상태, ADR-010).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(_CROCKFORD)}


def ulid_timestamp_ms(ulid: str) -> int:
    if not ULID_RE.match(ulid):
        raise ValueError(f"ULID가 아니다: {ulid!r}")
    value = 0
    for char in ulid[:10]:
        value = value * 32 + _DECODE[char]
    return value


def resume_start_time(cursor: str, slack_s: float) -> str:
    """커서 ULID → JetStream opt_start_time (RFC3339). slack만큼 되감는다."""
    at = datetime.fromtimestamp(ulid_timestamp_ms(cursor) / 1000, tz=UTC)
    return (at - timedelta(seconds=slack_s)).isoformat().replace("+00:00", "Z")
