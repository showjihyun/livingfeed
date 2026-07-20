"""gateway 설정 — 환경변수에서 조립한다 (dispatcher config와 동일 규약)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    nats_url: str
    env: str
    #: 세션 커맨드 적재용 — gateway는 player.* 이벤트의 발행 주체다 (ADR-017 §2)
    pg_dsn: str = "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
    #: 커맨드 적재 커넥션 풀 상한 — 세션이 커넥션을 붙잡지 않고 커맨드 단위로
    #: 대여하므로, 동시 in-flight 커맨드 수만큼만 있으면 된다 (접속자 수와 무관)
    pg_pool_max: int = 16
    #: 세션 프레즌스 저장 (ADR-010 — 무중단 드레이닝의 전제)
    redis_url: str = "redis://localhost:6379/0"
    #: WS 세션 공유 토큰 — 설정되면 /session 접속에 ?token= 또는
    #: Authorization: Bearer 일치를 요구한다.
    #: ⚠️ player_id는 아직 클라이언트 주장 값이다(계정 체계 부재) — 로컬 dev 밖에
    #: 노출한다면 반드시 설정하라. 검증된 신원에서 player_id를 도출하는 진짜
    #: 인증(계정/JWT)은 플레이어 계정 단계의 후속이다.
    session_token: str | None = None
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
    #: 페르소나 스튜디오 관리 API의 파일 디렉터리 — 파일이 SoT다 (ADR-001/012).
    #: 세계 반영은 tick 워커 핫 리로드의 몫 — gateway는 파일만 다룬다
    personas_dir: Path = field(default_factory=lambda: Path("agents/personas"))
    #: 관리 API 공유 토큰 — 설정되면 /admin/* 에 Authorization: Bearer 일치를
    #: 요구한다(403). 미설정(dev)은 열림 — 로컬 밖 노출 전 반드시 설정하라
    admin_token: str | None = None
    #: 서사 푸시(Web Push, plan/11 §D1) — 두 키가 모두 있어야 발송 워커가 켜진다
    #: (dev 기본 꺼짐). 키 생성(1회, 실키 커밋 금지 — env로만 주입하라):
    #:   uv run --package lf-gateway vapid --gen            # private/public_key.pem
    #:   uv run --package lf-gateway vapid --applicationServerKey  # 공개키(base64url)
    vapid_public_key: str | None = None  # FE 구독용 applicationServerKey (base64url)
    vapid_private_key: str | None = None  # private_key.pem 경로 또는 raw base64url 키
    #: 푸시 서비스에 보내는 연락처 (RFC 8292 — mailto: 필수)
    vapid_subject: str = "mailto:ops@livingfeed.local"
    #: 발송 워커 durable — 인스턴스가 여럿이어도 플레이어에게 알림은 한 번이다
    push_durable: str = "gateway-push"

    @classmethod
    def from_env(cls) -> Config:
        origins = os.environ.get("LF_CORS_ORIGINS", "http://localhost:3000")
        return cls(
            nats_url=os.environ.get("NATS_URL", "nats://localhost:4222"),
            env=os.environ.get("LF_ENV", "dev"),
            pg_dsn=os.environ.get(
                "LF_PG_DSN", "postgresql://livingfeed:livingfeed@localhost:5432/livingfeed"
            ),
            pg_pool_max=int(os.environ.get("LF_PG_POOL_MAX", "16")),
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            session_token=os.environ.get("LF_SESSION_TOKEN") or None,
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            personas_dir=Path(os.environ.get("LF_PERSONAS_DIR", "agents/personas")),
            admin_token=os.environ.get("LF_ADMIN_TOKEN") or None,
            vapid_public_key=os.environ.get("LF_VAPID_PUBLIC_KEY") or None,
            vapid_private_key=os.environ.get("LF_VAPID_PRIVATE_KEY") or None,
            vapid_subject=os.environ.get("LF_VAPID_SUBJECT", "mailto:ops@livingfeed.local"),
        )
