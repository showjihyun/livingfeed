"""FeedItem 승격 — 원본 사건 봉투 → feed.post.published NewEvent (ADR-014 §1단).

post_id는 원본 event_id에서 결정적으로 파생된 ULID다:
타임스탬프 48비트는 원본을 승계하고(커서 순서 = 사건 시간 순서),
난수부는 sha256(원본 id)로 고정한다. 같은 원본의 재전달은 같은 post_id가 되어
이벤트 스토어의 스트림 CAS(stream_key=post_id)가 중복 승격을 거부한다.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

import yaml
from lf_eventstore import NewEvent, Provenance

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
    "confess": ("{author}, {target}에게 마음을 고백하다", "{author}, 오래 품은 마음을 꺼내다"),
    "sever": ("{author}, {target}와의 관계를 끊다", "{author}, 관계 하나를 정리하다"),
    "move": ("{author}, {target}을 향해 자리를 옮기다", "{author}, 자리를 옮기다"),
    "work": ("{author}, 일에 몰두하다", "{author}, 일에 몰두하다"),
    "rest": ("{author}, 잠시 숨을 고르다", "{author}, 잠시 숨을 고르다"),
}
_TITLE_DEFAULT = ("{author}의 새로운 움직임", "{author}의 새로운 움직임")

#: 세계 사건(incident_kind)별 피드 제목 — Director 개입의 얼굴 (ADR-013/014)
_INCIDENT_TITLES: dict[str, str] = {
    "chance_encounter": "세계 뉴스 — 우연이 겹친 밤",
    "rumor_spread": "세계 뉴스 — 소문이 돌기 시작했다",
    "deadline_crunch": "세계 뉴스 — 모두의 마감이 당겨졌다",
    "sudden_rain": "세계 뉴스 — 갑작스런 폭우",
    "blackout": "세계 뉴스 — 도시가 어두워졌다",
}
_INCIDENT_TITLE_DEFAULT = "세계 뉴스 — 무슨 일이 일어났다"


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


def load_actor_communities(personas_dir: Path) -> dict[str, str]:
    """페르소나 id → 커뮤니티 id(^c_). 무소속·미상은 매핑에서 빠진다.

    컴포저가 포스트에 community_id를 실어 커뮤니티 피드(ADR-014)를 채우는 원천.
    소속은 페르소나 파일이 SoT다 (ADR-001/012) — 별도 상태 저장 없음.
    """
    communities: dict[str, str] = {}
    if not personas_dir.is_dir():
        return communities
    for path in sorted(personas_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            community = doc.get("community")
            if community:
                communities[doc["id"]] = community
        except Exception:  # 손상 파일이 피드를 멈추면 안 된다 (load_actor_names 선례)
            logger.warning("페르소나 파싱 실패(커뮤니티): %s — 건너뜀", path, exc_info=True)
    return communities


# \b는 못 쓴다 — 유니코드 \w에 한글이 들어가 'p_x에게'의 끝 경계가 성립하지 않는다.
# ASCII 영숫자가 앞에 붙은 경우만 배제한다 (grasp_… 같은 영단어 오염 방지)
_ACTOR_REF = re.compile(r"(?<![A-Za-z0-9])a_[a-z0-9_]+")
_PLAYER_REF = re.compile(r"(?<![A-Za-z0-9])p_[a-z0-9_]+")


def humanize_ids(text: str, actor_names: dict[str, str]) -> str:
    """화면 문장 정화 — 원시 id는 세계 어휘로 (리시트 정화의 결).

    LLM intent·서사가 컨텍스트의 식별자를 그대로 옮겨 적는 일이 있다 —
    액터 id는 실명 그라운딩(모르면 '누군가'), 플레이어 id는 세계 안
    어휘('어느 관찰자')로 바꾼다. 식별자는 화면 문장에 실리지 않는다.
    """
    text = _ACTOR_REF.sub(lambda m: actor_names.get(m.group(0), "누군가"), text)
    return _PLAYER_REF.sub("어느 관찰자", text)


def build_post_event(
    envelope: dict[str, Any],
    *,
    drama: float,
    score: float,
    actor_names: dict[str, str],
    community_id: str | None = None,
) -> NewEvent:
    """actor.action.performed 봉투를 feed.post.published NewEvent로 승격한다.

    가시성은 world다 — 세계는 한 마을이라 모두가 본다. 작성자가 커뮤니티에
    속하면 community_id를 함께 실어, 같은 글이 커뮤니티 피드(ADR-014)에도
    걸리게 한다 (커뮤니티는 월드의 부분집합 렌즈, 별도 가시성 강등이 아니다).
    """
    payload = envelope["payload"]
    author_id = envelope["actor_id"]
    target_id = payload.get("target_actor_id")
    author = actor_names.get(author_id, author_id)
    target = actor_names.get(target_id, target_id) if target_id else None

    # LLM이 지은 제목(headline)이 있으면 그것이 곧 헤드라인 — 이 순간에 맞는 서사 제목
    # (ADR-014 §제목 폴리시). 없으면 action_kind 템플릿으로 폴백한다.
    headline = (payload.get("headline") or "").strip()
    if headline:
        title, narration_kind = headline, "llm"
    else:
        with_target, without_target = _TITLES.get(payload["action_kind"], _TITLE_DEFAULT)
        title = (
            with_target.format(author=author, target=target)
            if target
            else without_target.format(author=author)
        )
        narration_kind = "template"

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
        # 승격은 원본을 옮겨 담을 뿐이다 — 본문이 곧 원본의 intent라, 그것이
        # LLM 생성물이면 포스트도 생성물이다 (ADR-021 §1 세탁 금지)
        provenance=Provenance.inherit(envelope, rule_id="feed.compose:action"),
        event_id=post_id,
        payload={
            "visibility": "world",
            "title": humanize_ids(title, actor_names)[:200],
            "body": humanize_ids(payload["intent"], actor_names)[:2000],
            "narration_kind": narration_kind,
            "participants": participants,
            "community_id": community_id,
            "location_id": payload.get("location_id"),
            "drama_score": round(drama, 4),
            "worthiness": round(score, 4),
            "source_event_type": envelope["type"],
            "tags": [payload["action_kind"]],
            "media": [],
        },
    )


#: 목표 완주는 서사의 큰 마디 — 대인 갈등에 준하는 드라마 (docs/plan/04 사슬의 끝)
GOAL_ACHIEVED_DRAMA = 0.8

#: 인생의 장이 넘어가는 순간 — 목표 완주와 같은 급의 서사 마디 (plan/08 Life Journey)
ARC_TRANSITION_DRAMA = 0.8

#: 인생 단계 → 피드 표기 라벨 (plan/08 닫힌 어휘 — 미지 코드는 코드 그대로)
_ARC_STAGE_LABELS: dict[str, str] = {
    "student": "학생기",
    "newcomer": "사회 초년기",
    "settling": "정착·방황기",
    "prime": "전성기·침체기",
    "elder": "원로기",
}


def evaluate_arc_transition(cfg: ScoringConfig) -> tuple[float, float]:
    """장 전환의 (drama, worthiness) — 희소한 마디 + 편집 부스트로 항상 승격."""
    score = worthiness(ARC_TRANSITION_DRAMA, 0.0, 1.0, 1.0, cfg)
    return ARC_TRANSITION_DRAMA, score


def build_arc_post_event(
    envelope: dict[str, Any],
    *,
    previous_stage: str | None,
    drama: float,
    score: float,
    actor_names: dict[str, str],
    community_id: str | None = None,
) -> NewEvent:
    """system.director.arc_planned(장 전환분만) → feed.post.published (ADR-014).

    아크 자체는 제어 신호라 피드가 아니다 — 승격되는 건 '장이 넘어가는 순간'뿐이다
    (같은 stage 재계획은 호출자가 거른다). 첫 아크는 이야기의 첫 장이 열린 것이다.
    """
    payload = envelope["payload"]
    target_id = payload["target_actor_id"]
    author = actor_names.get(target_id, target_id)
    stage = _ARC_STAGE_LABELS.get(payload["stage"], payload["stage"])
    if previous_stage is None:
        title = f"{author}, 이야기의 첫 장이 열리다"
        body = f"{author}의 삶이 '{stage}'에 접어들었다. {payload['intention']}"
    else:
        prev = _ARC_STAGE_LABELS.get(previous_stage, previous_stage)
        title = f"{author}, 인생의 장이 넘어가다"
        body = f"'{prev}'의 장이 닫히고 '{stage}'의 장이 열렸다. {payload['intention']}"
    post_id = derive_post_id(envelope["event_id"])
    return NewEvent(
        world_id=envelope["world_id"],
        stream="feed",
        stream_key=post_id,
        type=FEED_POST_TYPE,
        tick=envelope["tick"],
        actor_id=target_id,  # 장이 넘어간 건 그 인물의 삶이다
        causation_id=envelope["event_id"],
        correlation_id=envelope["correlation_id"],  # Director 계획의 사슬을 잇는다
        provenance=Provenance.inherit(envelope, rule_id="feed.compose:arc"),
        event_id=post_id,
        payload={
            "visibility": "world",
            "title": title[:200],
            "body": body[:2000],
            "narration_kind": "template",
            "participants": [target_id],
            "community_id": community_id,
            "location_id": None,
            "drama_score": round(drama, 4),
            "worthiness": round(score, 4),
            "source_event_type": envelope["type"],
            "tags": ["arc_transition", payload["stage"]],
            "media": [],
        },
    )


#: 새 인물의 등장 — 세계가 한 명을 받아들이는 순간 (페르소나 스튜디오의 방생)
DEBUT_DRAMA = 0.7


def evaluate_debut(cfg: ScoringConfig) -> tuple[float, float]:
    """데뷔의 (drama, worthiness) — 희소한 마디 + 편집 부스트로 항상 승격."""
    score = worthiness(DEBUT_DRAMA, 0.0, 1.0, 1.0, cfg)
    return DEBUT_DRAMA, score


def build_debut_post_event(
    envelope: dict[str, Any], *, drama: float, score: float, community_id: str | None = None
) -> NewEvent:
    """actor.identity.declared → feed.post.published — 데뷔는 세계의 사건이다.

    정체성은 세계당 1회 선언이라 도배가 없다. created_by(스튜디오 창조자)가
    있으면 포스트로 승계된다 — '당신이 빚은 인물' 저자성 표식의 원천 (plan/03).
    이름·소개는 선언 payload가 원천이라 명부 해석이 필요 없다.
    """
    payload = envelope["payload"]
    author_id = envelope["actor_id"]
    post_id = derive_post_id(envelope["event_id"])
    return NewEvent(
        world_id=envelope["world_id"],
        stream="feed",
        stream_key=post_id,
        type=FEED_POST_TYPE,
        tick=envelope["tick"],
        actor_id=author_id,
        causation_id=envelope["event_id"],
        correlation_id=envelope["correlation_id"],
        # 데뷔 소개문은 사람이 빚은 페르소나에서 온다 — 선언이 authored면
        # 포스트도 authored로 남아 '창발한 성격'과 구분된다 (ADR-021 §1)
        provenance=Provenance.inherit(envelope, rule_id="feed.compose:debut"),
        event_id=post_id,
        payload={
            "visibility": "world",
            "title": f"{payload['name']}, 세계에 첫발을 딛다"[:200],
            "body": payload["bio"][:2000],
            "narration_kind": "template",
            "participants": [author_id],
            "community_id": community_id,
            "location_id": None,
            "drama_score": round(drama, 4),
            "worthiness": round(score, 4),
            "source_event_type": envelope["type"],
            "tags": ["debut", payload["archetype"]],
            "created_by": payload.get("created_by"),
            "media": [],
        },
    )


def evaluate_goal_achievement(
    envelope: dict[str, Any], cfg: ScoringConfig
) -> tuple[float, float]:
    """목표 완주의 (drama, worthiness) — 희소한 마디 + 편집 부스트로 항상 승격."""
    score = worthiness(GOAL_ACHIEVED_DRAMA, 0.0, 1.0, 1.0, cfg)
    return GOAL_ACHIEVED_DRAMA, score


def build_goal_post_event(
    envelope: dict[str, Any], *, drama: float, score: float, actor_names: dict[str, str],
    community_id: str | None = None,
) -> NewEvent:
    """actor.goal.achieved → feed.post.published (인물의 마디, ADR-014)."""
    payload = envelope["payload"]
    author_id = envelope["actor_id"]
    author = actor_names.get(author_id, author_id)
    post_id = derive_post_id(envelope["event_id"])
    return NewEvent(
        world_id=envelope["world_id"],
        stream="feed",
        stream_key=post_id,
        type=FEED_POST_TYPE,
        tick=envelope["tick"],
        actor_id=author_id,
        causation_id=envelope["event_id"],
        correlation_id=envelope["correlation_id"],  # 목표를 이룬 행동의 사슬을 잇는다
        provenance=Provenance.inherit(envelope, rule_id="feed.compose:goal"),
        event_id=post_id,
        payload={
            "visibility": "world",
            "title": f"{author}, 오랜 목표를 이루다"[:200],
            "body": f"마침내 — {payload['description']}"[:2000],
            "narration_kind": "template",
            "participants": [author_id],
            "community_id": community_id,
            "location_id": None,
            "drama_score": round(drama, 4),
            "worthiness": round(score, 4),
            "source_event_type": envelope["type"],
            "tags": ["goal_achieved", payload["need"]],
            "media": [],
        },
    )


def evaluate(
    envelope: dict[str, Any],
    rarity: RarityTracker,
    cfg: ScoringConfig,
    *,
    director_boost: float = 0.0,
) -> tuple[float, float]:
    """원본 봉투의 (drama, worthiness)를 계산하고 희소성 창에 관측을 남긴다.

    director_boost는 boost_feed 편집 조명 (ADR-013/014) — Director가 지목한
    인물의 행동이 조명 기간 동안 worthiness boost 항을 얻는다.
    """
    payload = envelope["payload"]
    kind = payload["action_kind"]
    drama = drama_score(kind, has_target=payload.get("target_actor_id") is not None, cfg=cfg)
    # 희소성은 인물별 — 한 사람의 도배를 막되, 모두의 근황을 막지 않는다 (ADR-014).
    # kind 전역 키는 SNS 리듬(여럿이 각자 speak)에서 서로의 글을 깎아 피드를 굶긴다
    rarity_key = f"{envelope.get('actor_id')}:{kind}"
    score = worthiness(drama, 0.0, rarity.rarity(rarity_key), director_boost, cfg)
    rarity.observe(rarity_key)
    return drama, score


def evaluate_incident(
    envelope: dict[str, Any], rarity: RarityTracker, cfg: ScoringConfig
) -> tuple[float, float]:
    """세계 사건의 (drama, worthiness) — Director boost 항이 처음으로 실값이 된다.

    drama = 사건 강도, boost = 1.0 (Director가 놓은 사건이 곧 부스트 신호, ADR-014).
    """
    payload = envelope["payload"]
    kind = payload["incident_kind"]
    drama = min(1.0, max(0.0, float(payload["intensity"])))
    score = worthiness(drama, 0.0, rarity.rarity(kind), 1.0, cfg)
    rarity.observe(kind)
    return drama, score


def build_incident_post_event(
    envelope: dict[str, Any], *, drama: float, score: float
) -> NewEvent:
    """world.incident.occurred → feed.post.published (세계 뉴스 카드, ADR-014)."""
    payload = envelope["payload"]
    post_id = derive_post_id(envelope["event_id"])
    return NewEvent(
        world_id=envelope["world_id"],
        stream="feed",
        stream_key=post_id,
        type=FEED_POST_TYPE,
        tick=envelope["tick"],
        actor_id=None,  # 세계 사건 — 주체 액터가 없다
        causation_id=envelope["event_id"],
        correlation_id=envelope["correlation_id"],  # Director가 시작한 사슬을 잇는다
        provenance=Provenance.inherit(envelope, rule_id="feed.compose:incident"),
        event_id=post_id,
        payload={
            "visibility": "world",
            "title": _INCIDENT_TITLES.get(payload["incident_kind"], _INCIDENT_TITLE_DEFAULT),
            "body": payload["description"][:2000],
            "narration_kind": "template",
            "participants": payload["affected_actor_ids"],
            "community_id": None,
            "location_id": payload.get("location_id"),
            "drama_score": round(drama, 4),
            "worthiness": round(score, 4),
            "source_event_type": envelope["type"],
            "tags": [payload["incident_kind"]],
            "media": [],
        },
    )
