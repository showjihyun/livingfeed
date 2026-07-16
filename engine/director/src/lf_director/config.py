"""director 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    pg_dsn: str
    nats_url: str
    env: str
    world_id: str
    #: 침체 발화 tick 수/임계 재정의 (스모크·데모·시즌 튜닝용 — 기본은 params.yaml)
    quiet_ticks_override: int | None = None
    quiet_threshold_override: float | None = None
    #: LLM 개입 선택(director_plan) 활성화. False면 순수 규칙 — replay 결정성·비용 킬스위치
    llm_selection: bool = True
    #: 시즌 계획 케이던스(tick). 기본 360 = 세계 하루(1 tick=240 세계초). 저빈도 (ADR-013)
    season_interval_ticks: int = 360
    #: 개입 서술 그라운딩용 표시 이름 원천 (id→이름). 없으면 id로 표기
    personas_dir: Path = Path("agents/personas")
    observe_durable: str = "director-observe"
    sys_durable: str = "director-sys"
    #: Narrative Gravity — 플레이어 개입(시선의 원천) 구독 (ADR-013 §관찰)
    gravity_durable: str = "director-gravity"
    batch_size: int = 128
    fetch_timeout_s: float = 2.0

    @classmethod
    def from_env(cls) -> Config:
        quiet = os.environ.get("LF_DIRECTOR_QUIET_TICKS")
        threshold = os.environ.get("LF_DIRECTOR_QUIET_THRESHOLD")
        season = os.environ.get("LF_DIRECTOR_SEASON_TICKS")
        return cls(
            pg_dsn=os.environ.get(
                "LF_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
            ),
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            world_id=os.environ.get("LF_WORLD_ID", "w_main"),
            quiet_ticks_override=int(quiet) if quiet else None,
            quiet_threshold_override=float(threshold) if threshold else None,
            llm_selection=os.environ.get("LF_DIRECTOR_LLM", "1") != "0",
            personas_dir=Path(os.environ.get("LF_PERSONAS_DIR", "agents/personas")),
            season_interval_ticks=int(season) if season else 360,
        )
