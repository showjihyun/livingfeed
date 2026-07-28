"""WS 상호작용 세션 — 커맨드 검증·적재와 액터 응답 push (ADR-010 §WS).

메시지 봉투는 양방향 {type, seq, payload}다.
- 클라→서버: dm.send / comment.post / reaction.add / follow.set
- 서버→클라: ack(적재 확인) / error(명시적 거부 — 조용한 유실 금지) /
             actor.reply(actor.message.sent 봉투 push)

무상태 원칙(ADR-010): 발행은 append(player.*) → outbox relay가 유일 경로(ADR-017)이고,
응답은 LF_ACTOR의 actor.message.sent를 연결별 임시 consumer로 구독한다.
Redis에는 세션 프레즌스만 남는다 — 어느 gateway 인스턴스든 세션을 복원할 수 있다.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from lf_eventstore import (
    ConcurrencyConflict,
    NewEvent,
    Provenance,
    ValidationFailed,
    append,
    current_head,
)
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from psycopg import AsyncConnection

from lf_gateway.config import Config

PLAYER_RE = re.compile(r"^p_[a-z0-9_]+$")
WORLD_RE = re.compile(r"^w_[a-z0-9_]+$")

PRINCIPAL = "services.gateway"
REPLY_TYPE = "actor.message.sent"

#: 세션 커맨드 → (이벤트 타입, payload 필수 필드)
COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "dm.send": ("player.dm.sent", ("target_actor_id", "text")),
    "comment.post": ("player.comment.posted", ("target_actor_id", "post_id", "text")),
    "reaction.add": ("player.reaction.added", ("target_actor_id", "post_id", "kind")),
    # 팔로우는 세계에 남는 선언이다 (ADR-014 진짜 팔로우 모델) — 로컬 토글이 아니라
    "follow.set": ("player.follow.changed", ("target_actor_id", "following")),
}


class CommandError(Exception):
    """세션 커맨드 거부 사유 — error 프레임으로 돌려준다."""


def build_player_event(
    world_id: str, player_id: str, msg_type: str, payload: dict[str, Any]
) -> NewEvent:
    """세션 커맨드 → player.* NewEvent. 형식 오류는 CommandError."""
    command = COMMANDS.get(msg_type)
    if command is None:
        raise CommandError(f"알 수 없는 커맨드: {msg_type!r} (지원: {', '.join(sorted(COMMANDS))})")
    event_type, fields = command
    if not isinstance(payload, dict):
        raise CommandError("payload는 객체여야 한다")
    missing = [f for f in fields if f not in payload]
    if missing:
        raise CommandError(f"payload 필수 필드 누락: {', '.join(missing)}")

    data: dict[str, Any] = {"player_id": player_id}
    data.update({f: payload[f] for f in fields})
    # 포스트에 대한 개입은 그 포스트의 서사 사슬을 잇는다 (아크 추적, ADR-013) —
    # post_id는 feed.post.published의 event_id(ULID)이므로 correlation으로 쓸 수 있다.
    correlation = payload.get("post_id") if "post_id" in fields else None
    return NewEvent(
        world_id=world_id,
        stream="player",
        stream_key=player_id,
        type=event_type,
        # 플레이어 개입은 tick 밖의 사건이다 — tick 0 규약. 순서의 진실은
        # event_id(ULID)와 global_seq가 가진다 (redis-projector가 생기면 현재 tick 주입)
        tick=0,
        actor_id=None,
        correlation_id=correlation if isinstance(correlation, str) else None,
        # 사람이 직접 한 개입 — 세계가 지어낸 것과 결코 섞이면 안 된다 (ADR-021 §1)
        provenance=Provenance.authored(player_id),
        payload=data,
    )


async def handle_command(
    conn: AsyncConnection, world_id: str, player_id: str, raw: dict[str, Any]
) -> dict[str, Any]:
    """커맨드 하나를 적재하고 ack/error 프레임을 만든다."""
    seq = raw.get("seq")
    try:
        event = build_player_event(world_id, player_id, str(raw.get("type")), raw.get("payload"))
        head = await current_head(conn, world_id, "player", player_id)
        try:
            [stored] = await append(conn, PRINCIPAL, [event], expected_head=head)
        except ConcurrencyConflict:
            # 같은 플레이어의 다른 세션과 경합 — 재수화 후 1회 재시도 (ADR-005)
            head = await current_head(conn, world_id, "player", player_id)
            [stored] = await append(conn, PRINCIPAL, [event], expected_head=head)
    except CommandError as e:
        return {"type": "error", "seq": seq, "payload": {"message": str(e)}}
    except ValidationFailed as e:
        return {"type": "error", "seq": seq, "payload": {"message": f"스키마 위반: {e}"}}
    return {
        "type": "ack",
        "seq": seq,
        "payload": {"event_id": stored.envelope["event_id"]},
    }


async def open_reply_subscription(js: JetStreamContext, cfg: Config, world_id: str):
    """액터 응답 push용 임시 consumer — 접속 이후 신규분만 (재개는 세션 재전송이 아니라
    '받은 것' 히스토리 조회의 몫이다, Phase 후속)."""
    subject = f"lf.{cfg.env}.{world_id}.{REPLY_TYPE}"
    consumer = ConsumerConfig(deliver_policy=DeliverPolicy.NEW, ack_policy=AckPolicy.NONE)
    return await js.subscribe(subject, stream="LF_ACTOR", config=consumer)


def reply_frame_for(data: bytes, player_id: str, seq: int) -> dict[str, Any] | None:
    """수신 actor.message.sent → push 대상이면 프레임, 아니면 None.

    두 경로가 push된다: (1) 나를 향한 응답 (target_player_id == 나),
    (2) 액터→액터 댓글 (액터 소셜 루프 — target_actor_id·post_id가 있는 world
    포스트의 공개 대화라 모든 세션이 본다; FE가 post_id로 인라인 렌더한다).
    """
    envelope = json.loads(data)
    payload = envelope.get("payload", {})
    mine = payload.get("target_player_id") == player_id
    actor_comment = (
        payload.get("channel") == "comment"
        and payload.get("target_actor_id")
        and payload.get("post_id")
    )
    if not (mine or actor_comment):
        return None
    return {"type": "actor.reply", "seq": seq, "payload": envelope}


def presence_key(world_id: str, player_id: str) -> str:
    return f"lf:session:{world_id}:{player_id}"


def presence_value(last_seq: Any) -> str:
    return json.dumps(
        {"last_seq": last_seq, "seen_at": datetime.now(UTC).isoformat()}, ensure_ascii=False
    )
