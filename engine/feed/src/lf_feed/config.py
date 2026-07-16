"""feed composer 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from lf_feed.scoring import ScoringConfig


@dataclass(frozen=True)
class Config:
    pg_dsn: str
    nats_url: str
    env: str
    personas_dir: Path
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    #: JetStream durable 이름 — 재시작 시 오프셋을 이어받는다
    durable: str = "feed-composer"
    source_stream: str = "LF_ACTOR"
    batch_size: int = 64
    fetch_timeout_s: float = 5.0
    #: 이 횟수 이상 재전달된 처리 불가 메시지는 DLQ로 보내고 ack한다 (조용한 유실 금지, ADR-017 §4)
    max_deliver: int = 5
    nak_delay_s: float = 5.0
    #: LLM 내레이션 (ADR-018 narrate) — 켜면 고드라마 포스트 본문을 서사로 다듬는다.
    #: rule 프로바이더는 미지원이라 dev 기본 꺼짐 (결정성 유지)
    narrate: bool = False
    narrate_threshold: float = 0.6

    @classmethod
    def from_env(cls) -> Config:
        scoring = ScoringConfig()
        if "LF_FEED_THRESHOLD" in os.environ:
            scoring = ScoringConfig(threshold=float(os.environ["LF_FEED_THRESHOLD"]))
        return cls(
            pg_dsn=os.environ.get(
                "LF_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
            ),
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            personas_dir=Path(os.environ.get("LF_PERSONAS_DIR", "agents/personas")),
            scoring=scoring,
            narrate=os.environ.get("LF_FEED_NARRATE", "0") == "1",
            narrate_threshold=float(os.environ.get("LF_FEED_NARRATE_THRESHOLD", "0.6")),
        )
