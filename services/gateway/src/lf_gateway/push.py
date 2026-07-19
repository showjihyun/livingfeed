"""서사 푸시 알림 — actor.message.sent를 자리 비운 플레이어의 브라우저로 (plan/11 §D1).

소스가 target_player_id 있는 actor.message.sent(댓글 답장·DM·선제 DM)뿐이므로
"알림은 반드시 그 유저와 관련된 서사"라는 §D1 원칙이 구조적으로 보장된다 —
범용 공지를 실을 경로 자체가 없다.

정책 (plan/11 §알림 정책 — 신뢰 자산 보호):
- 접속 중(프레즌스 有) 플레이어에겐 보내지 않는다 — 이미 WS로 보고 있다.
- 문구는 담백한 서사형("누가·무슨 일") + 본문 80자 — 죄책감·허위 긴급성 금지.
- 410/404 응답의 구독은 자동 제거(만료된 브라우저), 그 외 실패는 로그만 —
  전달 채널이 세계를 멈추지 않는다.

구독은 상태가 아니라 전달 채널이다 — es 적재 없이 Redis hash에만 산다:
    lf:push:{world_id}:{player_id} = { endpoint → PushSubscription JSON }
플레이어당 다중 브라우저를 endpoint 필드 키가 흡수한다.

발송 워커는 LF_ACTOR durable(gateway-push) pull consumer — gateway 인스턴스가
여럿이어도 발송은 한 번이다. VAPID 키(config 참고)가 없으면 워커는 켜지지 않는다.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import nats.errors
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, DeliverPolicy
from pydantic import BaseModel, ConfigDict, Field

from lf_gateway.config import Config
from lf_gateway.session import presence_key

logger = logging.getLogger("lf.gateway.push")

PUSH_SOURCE_TYPE = "actor.message.sent"
#: 본문 미리보기 길이 — 알림은 예고편이지 본편이 아니다
PREVIEW_LEN = 80
FETCH_BATCH = 50
FETCH_TIMEOUT_S = 5.0
#: 발송 TTL — 하루 지난 답장 알림은 신선하지 않다 (양치기 소년 방지)
PUSH_TTL_S = 24 * 3600
#: 미지 액터 폴백 — gateway는 명부의 주인이 아니다 (director/composer 선례)
UNKNOWN_NAME = "누군가"

#: 발송 함수 시그니처 — (subscription, note). 소멸(410/404)은 SubscriptionGone.
SendFn = Callable[[dict[str, Any], dict[str, str]], Awaitable[None]]


class SubscriptionGone(Exception):
    """구독 소멸(410/404) — 저장소에서 자동 제거하라는 신호."""


def push_key(world_id: str, player_id: str) -> str:
    return f"lf:push:{world_id}:{player_id}"


# ── 문구 빌더 (순수) — 서사형, 다크 패턴 금지 ────────────────────────────────


def subject_particle(name: str) -> str:
    """이/가 조사 — 받침 유무. 한글 밖 이름은 '이(가)' 폴백."""
    code = ord(name[-1]) if name else 0
    if 0xAC00 <= code <= 0xD7A3:
        return "이" if (code - 0xAC00) % 28 else "가"
    return "이(가)"


def clip_preview(text: str) -> str:
    text = " ".join(text.split())  # 줄바꿈·연속 공백은 알림 한 줄로 접는다
    return text if len(text) <= PREVIEW_LEN else text[:PREVIEW_LEN] + "…"


def build_notification(
    envelope: dict[str, Any], name_of: Callable[[str | None], str]
) -> dict[str, str] | None:
    """actor.message.sent 봉투 → {title, body}. 푸시 대상이 아니면 None.

    문구는 서사형 두 결뿐이다: dm은 "{이름}의 안부가 도착했어요",
    댓글 답장은 "{이름}이/가 당신에게 답했어요" (plan/11 §알림 정책 1·4).
    """
    payload = envelope.get("payload") or {}
    if not payload.get("target_player_id"):
        return None  # 액터→액터 대화는 그 유저의 서사가 아니다
    name = name_of(envelope.get("actor_id"))
    if payload.get("channel") == "dm":
        title = f"{name}의 안부가 도착했어요"
    else:
        title = f"{name}{subject_particle(name)} 당신에게 답했어요"
    return {"title": title, "body": clip_preview(str(payload.get("text", "")))}


class ActorNames:
    """페르소나 id → 표시 이름 (LF_PERSONAS_DIR 로더 — director/composer 선례).

    스튜디오가 세계 도중에 인물을 만든다 — 미스가 나면 재스캔한다
    (최소 간격으로, 파일 몇 개 스캔이라 싸다). 그래도 모르면 '누군가'.
    """

    def __init__(self, personas_dir: Path, reload_interval_s: float = 60.0) -> None:
        self._dir = personas_dir
        self._interval = reload_interval_s
        self._names: dict[str, str] = {}
        self._loaded_at = 0.0
        self._load()

    def _load(self) -> None:
        self._loaded_at = time.monotonic()
        if not self._dir.is_dir():
            logger.warning("페르소나 디렉터리 없음: %s — '%s'로 표기한다", self._dir, UNKNOWN_NAME)
            return
        for path in sorted(self._dir.glob("*.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                self._names[doc["id"]] = doc["name"]
            except Exception:  # 페르소나 파일 손상이 발송을 멈추면 안 된다
                logger.warning("페르소나 파싱 실패: %s — 건너뜀", path, exc_info=True)

    def get(self, actor_id: str | None) -> str:
        if actor_id and actor_id not in self._names:
            if time.monotonic() - self._loaded_at >= self._interval:
                self._load()  # 스튜디오 신생 인물일 수 있다
        return self._names.get(actor_id or "", UNKNOWN_NAME)


# ── 구독 저장 API — es 적재 없음 (구독은 상태가 아니라 전달 채널) ─────────────


class PushKeysDoc(BaseModel):
    p256dh: str = Field(min_length=1)
    auth: str = Field(min_length=1)


class PushSubscriptionDoc(BaseModel):
    """표준 PushSubscription.toJSON() — 미지 키(expirationTime 등)는 보존한다."""

    model_config = ConfigDict(extra="allow")

    endpoint: str = Field(pattern=r"^https://")
    keys: PushKeysDoc


class SubscribeBody(BaseModel):
    player_id: str = Field(pattern=r"^p_[a-z0-9_]+$")
    world_id: str = Field("w_main", pattern=r"^w_[a-z0-9_]+$")
    subscription: PushSubscriptionDoc


class UnsubscribeBody(BaseModel):
    player_id: str = Field(pattern=r"^p_[a-z0-9_]+$")
    world_id: str = Field("w_main", pattern=r"^w_[a-z0-9_]+$")
    endpoint: str = Field(pattern=r"^https://")


def create_push_router(cfg: Config) -> APIRouter:
    router = APIRouter(prefix="/push")

    async def require_session(request: Request) -> None:
        # 구독 저장/해지는 /session WS와 같은 게이트를 건다 — 인증 없이 열려 있으면
        # 공격자가 남의 player_id로 자기 endpoint를 심어 그 유저의 DM 미리보기를
        # 가로챌 수 있다 (plan/11 §알림 정책 — 신뢰 자산). 미설정(dev)은 열림.
        if cfg.session_token is None:
            return  # dev 개방 — 로컬 밖 노출 전 LF_SESSION_TOKEN을 설정하라
        supplied = request.query_params.get("token")
        if supplied is None:
            scheme, _, credential = request.headers.get("authorization", "").partition(" ")
            supplied = credential.strip() if scheme.lower() == "bearer" else ""
        if not hmac.compare_digest(supplied, cfg.session_token):
            raise HTTPException(403, "세션 토큰이 필요하다 (LF_SESSION_TOKEN)")

    @router.get("/vapid-key")
    async def vapid_key() -> dict[str, str]:
        """구독에 쓸 applicationServerKey — 미설정이면 404 (FE는 조용한 강등).

        공개키(applicationServerKey)는 본디 브라우저에 실리는 값이라 게이트 밖에 둔다 —
        사설 데이터가 아니다. 게이트는 구독을 심고 지우는 경로(아래)에만 건다.
        """
        if not cfg.vapid_public_key:
            raise HTTPException(404, "서사 푸시가 꺼져 있다 (LF_VAPID_PUBLIC_KEY 미설정)")
        return {"key": cfg.vapid_public_key}

    @router.post("/subscribe", status_code=204, dependencies=[Depends(require_session)])
    async def subscribe(body: SubscribeBody, request: Request) -> None:
        sub = body.subscription
        await request.app.state.redis.hset(
            push_key(body.world_id, body.player_id),
            sub.endpoint,
            json.dumps(sub.model_dump(), ensure_ascii=False),
        )

    @router.delete("/subscribe", dependencies=[Depends(require_session)])
    async def unsubscribe(body: UnsubscribeBody, request: Request) -> dict[str, bool]:
        removed = await request.app.state.redis.hdel(
            push_key(body.world_id, body.player_id), body.endpoint
        )
        return {"removed": bool(removed)}

    return router


# ── 발송 — 프레즌스 제외·소멸 구독 자동 제거 ────────────────────────────────


async def deliver(
    envelope: dict[str, Any], *, redis: Any, names: ActorNames, send: SendFn
) -> int:
    """봉투 하나 처리 — 발송 시도 수 반환 (관측용).

    프레즌스에 있는(접속 중) 플레이어는 건너뛴다 — 이미 WS로 보고 있는데
    알림까지 울리면 알림의 신뢰가 닳는다 (plan/11 §알림 정책 3).
    """
    note = build_notification(envelope, names.get)
    if note is None:
        return 0
    world_id = envelope.get("world_id", "")
    player_id = envelope["payload"]["target_player_id"]
    if await redis.exists(presence_key(world_id, player_id)):
        return 0
    key = push_key(world_id, player_id)
    sent = 0
    for endpoint, raw in (await redis.hgetall(key)).items():
        try:
            await send(json.loads(raw), note)
            sent += 1
        except SubscriptionGone:
            await redis.hdel(key, endpoint)  # 만료된 브라우저 — 유령 구독은 지운다
            logger.info("소멸 구독 제거(410/404): player=%s", player_id)
        except Exception as e:
            logger.warning("웹 푸시 발송 실패(무시): player=%s %s", player_id, e)
    return sent


def make_sender(cfg: Config) -> SendFn:
    """pywebpush 발송 함수 — VAPID 서명. 동기 HTTP라 스레드로 내린다."""
    from pywebpush import WebPushException, webpush  # 지연 임포트 — 워커 꺼짐이면 안 탄다

    def send_sync(subscription: dict[str, Any], note: dict[str, str]) -> None:
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(note, ensure_ascii=False),
                vapid_private_key=cfg.vapid_private_key,
                vapid_claims={"sub": cfg.vapid_subject},
                ttl=PUSH_TTL_S,
            )
        except WebPushException as e:
            if getattr(e.response, "status_code", None) in (404, 410):
                raise SubscriptionGone(str(e)) from e
            raise

    async def send(subscription: dict[str, Any], note: dict[str, str]) -> None:
        await asyncio.to_thread(send_sync, subscription, note)

    return send


async def run_push_worker(
    js: JetStreamContext, redis: Any, cfg: Config, send: SendFn | None = None
) -> None:
    """lifespan 발송 워커 — LF_ACTOR durable pull 소비 (composer 선례).

    DeliverPolicy.NEW: durable 최초 생성 시 과거 7일치를 재생하지 않는다 —
    첫 배포가 알림 폭풍이 되면 채널은 그날로 죽는다. 이후 재시작은 ack 지점부터.
    처리 실패도 ack다(로그만) — 전달 채널에 재시도 폭풍·DLQ는 과하다.
    """
    names = ActorNames(cfg.personas_dir)
    send = send or make_sender(cfg)
    psub = await js.pull_subscribe(
        f"lf.{cfg.env}.*.{PUSH_SOURCE_TYPE}",
        durable=cfg.push_durable,
        stream="LF_ACTOR",
        config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
    )
    logger.info("서사 푸시 워커 대기 — durable=%s subject=lf.%s.*.%s",
                cfg.push_durable, cfg.env, PUSH_SOURCE_TYPE)
    while True:
        try:
            msgs = await psub.fetch(FETCH_BATCH, timeout=FETCH_TIMEOUT_S)
        except (TimeoutError, nats.errors.TimeoutError):
            continue  # 유휴 — 정상 흐름
        for msg in msgs:
            try:
                await deliver(json.loads(msg.data), redis=redis, names=names, send=send)
            except Exception:
                logger.warning("푸시 처리 실패(무시): subject=%s", msg.subject, exc_info=True)
            await msg.ack()
