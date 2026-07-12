"""gateway 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약)."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    nats_url: str
    env: str
    #: 브라우저 오리진 허용 목록 (EventSource/fetch CORS) — 쉼표 구분
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    stream: str = "LF_FEED"
    #: SSE 유휴 하트비트 — 프록시의 idle 종료 방지 (ADR-010)
    heartbeat_s: float = 15.0
    #: 커서 재개 시 ULID 타임스탬프에서 되감는 여유 — 발행 지연 흡수 (놓침 방지,
    #: 중복은 커서 비교가 걸러낸다)
    resume_slack_s: float = 60.0
    #: 롱폴링 최대 대기 (SSE 불가 환경 폴백, ADR-010)
    poll_max_wait_s: float = 25.0
    poll_batch: int = 100

    @classmethod
    def from_env(cls) -> Config:
        origins = os.environ.get("LF_CORS_ORIGINS", "http://localhost:3000")
        return cls(
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        )
