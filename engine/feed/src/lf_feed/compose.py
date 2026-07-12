"""FeedItem 승격 — 원본 사건 봉투 → feed.post.published NewEvent (ADR-014 §1단).

post_id는 원본 event_id에서 결정적으로 파생된 ULID다:
타임스탬프 48비트는 원본을 승계하고(커서 순서 = 사건 시간 순서),
난수부는 sha256(원본 id)로 고정한다. 같은 원본의 재전달은 같은 post_id가 되어
이벤트 스토어의 스트림 CAS(stream_key=post_id)가 중복 승격을 거부한다.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml
from lf_eventstore import NewEvent

from lf_feed.scoring import RarityTracker, ScoringConfig, drama_score, worthiness

logger = logging.getLogger("lf.feed.compose")

FEED_POST_TYPE = "feed.post.published"
PRINCIPAL = "engine.feed"

#: Crockford Base32 — ULID 알파벳 (envelope 패턴 ^[0-9A-HJKMNP-TV-Z]{26}$)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: action_kind별 제목 템플릿 — (대상 있음, 대상 없음). 본문은 intent 원문이다.
_TITLES: dict[str, tuple[str, str]] = {
    "speak": ("{author}, {target}에게 말을 걸다", "{author}, 이야기를 꺼내다"),
    "confront": ("{author}, {target}에게 맞서다", "{author}, 갈등의 한복판에 서다"),
    "help": ("{author}, {target}에게 손을 내밀다", "{author}, 도움의 손길을 내밀다"),
    "move": ("{author}, {target}을 향해 자리를 옮기다", "{author}, 자리를 옮기다"),
    "work": ("{author}, 일에 몰두하다", "{author}, 일에 몰두하다"),
    "rest": ("{author}, 잠시 숨을 고르다", "{author}, 잠시 숨을 고르다"),
}
_TITLE_DEFAULT = ("{author}의 새로운 움직임", "{author}의 새로운 움직임")


def derive_post_id(source_event_id: str) -> str:
    """원본 event_id → 결정적 post ULID (타임스탬프 승계 + 해시 난수부)."""
    digest = hashlib.sha256(f"feed.post:{source_event_id}".encode()).digest()
    random_part = "".join(_CROCKFORD[b % 32] for b in digest[:16])
    return source_event_id[:10] + random_part


def load_actor_names(personas_dir: Path) -> dict[str, str]:
    """페르소나 id → 표시 이름. 디렉터리가 없으면 빈 매핑 (id로 표기)."""
    names: dict[str, str] = {}
    if not personas_dir.is_dir():
        logger.warning("페르소나 디렉터리 없음: %s — 액터 id로 표기한다", personas_dir)
        return names
    for path in sorted(personas_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            names[doc["id"]] = doc["name"]
        except Exception:  # 페르소나 파일 손상이 피드를 멈추면 안 된다
            logger.warning("페르소나 파싱 실패: %s — 건너뜀", path, exc_info=True)
    return names


def build_post_event(
    envelope: dict[str, Any],
    *,
    drama: float,
    score: float,
    actor_names: dict[str, str],
) -> NewEvent:
    """actor.action.performed 봉투를 feed.post.published NewEvent로 승격한다.

    가시성은 MVP에서 전부 world다 — 관계/커뮤니티 가시성은 해당 엔진
    (ADR-016, Community)이 생길 때 규칙을 추가한다 (등급은 FeedItem 속성이므로
    이 함수의 반환값만 달라지면 된다, ADR-014).
    """
    payload = envelope["payload"]
    author_id = envelope["actor_id"]
    target_id = payload.get("target_actor_id")
    author = actor_names.get(author_id, author_id)
    target = actor_names.get(target_id, target_id) if target_id else None

    with_target, without_target = _TITLES.get(payload["action_kind"], _TITLE_DEFAULT)
    title = (
        with_target.format(author=author, target=target)
        if target
        else without_target.format(author=author)
    )

    participants = [author_id] + ([target_id] if target_id else [])
    post_id = derive_post_id(envelope["event_id"])
    return NewEvent(
        world_id=envelope["world_id"],
        stream="feed",
        # 포스트가 곧 엔티티다 — 포스트별 스트림이라 CAS 경합이 없고,
        # 재전달은 같은 post_id의 head 충돌(ConcurrencyConflict)로 걸러진다
        stream_key=post_id,
        type=FEED_POST_TYPE,
        tick=envelope["tick"],
        actor_id=author_id,
        causation_id=envelope["event_id"],
        # 서사 사슬 승계 — 아크 링킹과 다양성 보정의 키 (ADR-013/014)
        correlation_id=envelope["correlation_id"],
        event_id=post_id,
        payload={
            "visibility": "world",
            "title": title[:200],
            "body": payload["intent"][:2000],
            "narration_kind": "template",
            "participants": participants,
            "community_id": None,
            "location_id": payload.get("location_id"),
            "drama_score": round(drama, 4),
            "worthiness": round(score, 4),
            "source_event_type": envelope["type"],
            "tags": [payload["action_kind"]],
            "media": [],
        },
    )


def evaluate(
    envelope: dict[str, Any], rarity: RarityTracker, cfg: ScoringConfig
) -> tuple[float, float]:
    """원본 봉투의 (drama, worthiness)를 계산하고 희소성 창에 관측을 남긴다."""
    payload = envelope["payload"]
    kind = payload["action_kind"]
    drama = drama_score(kind, has_target=payload.get("target_actor_id") is not None, cfg=cfg)
    score = worthiness(drama, 0.0, rarity.rarity(kind), 0.0, cfg)
    rarity.observe(kind)
    return drama, score
