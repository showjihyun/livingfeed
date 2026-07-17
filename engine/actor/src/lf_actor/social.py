"""액터 소셜 루프 — 피드 순환의 배달 대상 선정 (순수 + 관계 조회 어댑터).

라우터(mailbox.run_mailbox_router)가 쓰는 두 판단을 소유한다:
  1. feed.post.published를 누구에게 배달하나 — 작성자와 관계 있는 액터 중
     salience 상위 N, 관계가 하나도 없으면 결정적 해시로 N명 (새 인물도
     이웃이 생기게). 결정적이다 — 같은 (포스트, 관계 상태)면 같은 이웃.
  2. 액터 댓글(actor.message.sent)을 누구에게 배달하나 — 글 작성자 1명뿐.
     작성자의 답글(in_reply_to ≠ post_id)은 배달하지 않는다 — 깊이 1 종결.
수치(배달 이웃 수·댓글 상한)는 params.yaml social 절이 원천이다.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import Sequence
from typing import Any

from lf_actor.relationship import RelationshipAdapter
from lf_actor.rhythm import default_params

logger = logging.getLogger("lf.actor.social")

FEED_POST_TYPE = "feed.post.published"


def fanout_targets(
    author_id: str,
    post_id: str,
    roster: Sequence[str],
    saliences: dict[str, float],
    *,
    limit: int,
) -> list[str]:
    """포스트 배달 대상 — 관계 salience 상위 N, 관계 전무 시 결정적 해시 N (순수).

    saliences는 '독자→작성자' 엣지의 salience다 (독자의 마음속 작성자의 비중).
    관계가 하나라도 있으면 그들에게만 간다 — 관계가 곧 이웃이다. 하나도 없으면
    (post_id, 독자) 해시로 N명을 고른다 — 새 인물의 글도 누군가는 본다.
    """
    candidates = [a for a in roster if a != author_id]
    related = sorted(
        (a for a in candidates if a in saliences),
        key=lambda a: (-saliences[a], a),
    )
    if related:
        return related[:limit]
    hashed = sorted(candidates, key=lambda a: (zlib.crc32(f"{post_id}:{a}".encode()), a))
    return hashed[:limit]


def comment_targets(envelope: dict[str, Any]) -> list[str]:
    """액터 댓글의 배달 대상 — 글 작성자 1명, 깊이 1에서 끝 (순수).

    최상위 댓글만 배달한다: in_reply_to == post_id (글 자체에 단 댓글).
    작성자의 답글은 in_reply_to가 댓글 event_id라 여기서 걸러진다 — 댓글이
    다시 의무를 만들지 않는다 (루프 차단). 플레이어 대상 응답(dm·플레이어
    댓글 답장)은 세션/타임라인 경로의 몫이다.
    """
    payload = envelope["payload"]
    if payload.get("channel") != "comment":
        return []
    target = payload.get("target_actor_id")
    if not target or target == envelope.get("actor_id"):
        return []
    if payload.get("in_reply_to") != payload.get("post_id"):
        return []  # 답글의 답글은 돌지 않는다 — 깊이 1 종결
    return [target]


def with_comment_path(schema: dict[str, Any], seen_posts: list[dict[str, Any]]) -> dict[str, Any]:
    """행동 스키마에 선택적 comment 응답 경로를 연다 (순수).

    post_id는 실제로 본 글의 id로 닫는다(enum) — 환각 포스트에 댓글이 달리지
    않는다. comment는 선택·nullable이다: 마음이 안 움직이면 안 남긴다 (LLM의 몫).
    원본 스키마는 불변 — 확정 이벤트(actor.action.performed)는 원본 스키마로
    검증되므로 comment는 적재 전에 반드시 분리된다 (extract_comment).
    """
    post_ids = [post["event_id"] for post in seen_posts]
    if not post_ids:
        return schema
    return {
        **schema,
        "properties": {
            **schema.get("properties", {}),
            "comment": {
                "type": ["object", "null"],
                "description": "방금 본 피드 글에 남기는 짧은 답글 — 마음이 움직였을 때만",
                "properties": {
                    "post_id": {"type": "string", "enum": post_ids},
                    "text": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["post_id", "text"],
                "additionalProperties": False,
            },
        },
    }


def extract_comment(
    payload: dict[str, Any], seen_posts: list[dict[str, Any]]
) -> tuple[dict[str, Any], tuple[dict[str, Any], str] | None]:
    """LLM 행동 payload에서 comment를 분리한다 (순수).

    반환: (comment가 제거된 payload, (대상 포스트 봉투, 댓글 텍스트) 또는 None).
    본 글 목록에 없는 post_id·빈 텍스트는 조용히 버린다 — 환각은 소스에서 끊는다
    (sanitize_target 규약). payload는 불변 — 새 dict를 돌려준다.
    """
    comment = payload.get("comment")
    cleaned = {k: v for k, v in payload.items() if k != "comment"}
    if not isinstance(comment, dict):
        return cleaned, None
    text = str(comment.get("text") or "").strip()
    post = next(
        (p for p in seen_posts if p["event_id"] == comment.get("post_id")), None
    )
    if not text or post is None or not post.get("actor_id"):
        return cleaned, None
    return cleaned, (post, text)


class FeedFanout:
    """feed.post.published → 배달 대상 액터들 (관계 엣지 Redis 조회 + 순수 선정).

    독자 후보는 이 워커의 페르소나 명부다 (Phase 1: 소수 액터 — 명부 순회가
    유계다). visibility가 world인 포스트만 순환한다 — 비공개는 배달하지 않는다.
    """

    def __init__(
        self,
        relationship: RelationshipAdapter,
        roster: Sequence[str],
        *,
        limit: int | None = None,
    ) -> None:
        self._relationship = relationship
        self._roster = list(roster)
        self._limit = int(
            default_params()["social"]["feed_fanout"] if limit is None else limit
        )

    def set_roster(self, roster: Sequence[str]) -> None:
        """리로드된 명부 반영 — 새 액터도 피드 순환의 독자·이웃이 된다 (핫 리로드)."""
        self._roster = list(roster)

    async def targets(self, envelope: dict[str, Any]) -> list[str]:
        payload = envelope["payload"]
        if payload.get("visibility") != "world":
            return []  # 비공개 글은 순환하지 않는다
        author = envelope.get("actor_id")
        if not author:
            return []  # 세계 뉴스(작성자 없음)는 incident 경로가 이미 지각시킨다
        world_id = envelope["world_id"]
        saliences: dict[str, float] = {}
        for reader in self._roster:
            if reader == author:
                continue
            state = await self._relationship.load(world_id, reader, author)
            if state is not None:
                saliences[reader] = state.salience
        chosen = fanout_targets(
            author, envelope["event_id"], self._roster, saliences, limit=self._limit
        )
        if chosen:
            logger.info("피드 순환: %s의 글 → %s", author, ", ".join(chosen))
        return chosen
