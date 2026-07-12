"""projector 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from lf_projector.os_index import DEFAULT_INDEX


@dataclass(frozen=True)
class Config:
    nats_url: str
    opensearch_url: str
    env: str
    index: str = DEFAULT_INDEX
    #: JetStream durable 이름 — 프로젝터 체크포인트 (ADR-003 계약 2)
    durable: str = "os-projector"
    stream: str = "LF_FEED"
    #: kuzu-projector — 임베디드 그래프 DB 디렉터리 (세계당 하위 디렉터리, ADR-006)
    kuzu_dir: str = "data/kuzu"
    kuzu_durable: str = "kuzu-projector"
    batch_size: int = 128
    fetch_timeout_s: float = 5.0
    #: 이 횟수 이상 재전달된 처리 불가 메시지는 DLQ로 보내고 ack한다 (ADR-017 §4)
    max_deliver: int = 5
    nak_delay_s: float = 5.0

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            opensearch_url=os.environ.get("OPENSEARCH_URL", "http://localhost:9200"),
            env=os.environ.get("LF_ENV", "dev"),
            kuzu_dir=os.environ.get("LF_KUZU_DIR", "data/kuzu"),
        )
