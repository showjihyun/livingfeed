"""ULID 생성 — 48비트 ms 타임스탬프 + 80비트 난수, Crockford base32 26자.

envelope 스키마의 event_id 패턴(^[0-9A-HJKMNP-TV-Z]{26}$)과 호환된다 (ADR-002).
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(timestamp_ms: int | None = None) -> str:
    ts = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    if not 0 <= ts < 1 << 48:
        raise ValueError(f"타임스탬프가 48비트 범위를 벗어난다: {ts}")
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_ALPHABET[(value >> shift) & 0x1F] for shift in range(125, -1, -5))
