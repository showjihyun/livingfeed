"""서사 사슬 조회 — "이 이야기의 시작점" (plan/03 §단계 3→4, ADR-002 인과 체인).

내 개입에서 시작된 사건 연쇄를 correlation_id 하나로 따라간다. 실세의 보상은
권력이 아니라 저자성이다 — origin이 요청자 자신이면 started_by_you로 알린다.

es.events 직접 읽기는 읽기 API 규칙(ADR-003: 프로젝션만 읽는다)의 문서화된
예외다: 인과 사슬은 원본(es)이 곧 정본이고, 사슬 전용 프로젝션은 원본의
복사본에 지나지 않는다 — projector replay/verify가 같은 이유로 es를 읽는 선례.
여기서는 read-only SELECT만 한다. 쓰기는 여전히 lf-eventstore append 단일 경로다.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from lf_feed_api.sanitize import find_actor_refs, humanize_ids

logger = logging.getLogger("lf.feed_api.story")

# ── 무서사 제외 — 블랙리스트 ──────────────────────────────────────────
# 블랙리스트인 이유: 새 이벤트 타입은 기본 포함이다. 화이트리스트 누락으로
# 이야기의 마디가 조용히 사라지는 것보다, 낯선 항목이 일반 문장(구조 필드
# type이 관찰 좌표)으로 드러나는 쪽이 관찰로 고치기 쉽다 (침묵 실패 회피).
# 타입 라벨을 화면 문장에 싣는 폴백은 기계 어휘 유출이라 걷어냈다 (summarize).

#: 세계의 심장박동 — 매 tick 반복되는 기계 신호라 서사가 아니다
_NON_NARRATIVE_PREFIXES: tuple[str, ...] = ("system.tick.",)

#: 무대 장치 노브 — payload가 수치 조정뿐이라 사람이 읽을 문장이 없다.
#: (system.director.intervened/arc_planned는 reason/intention 문장이 있어 남긴다)
_NON_NARRATIVE_TYPES = frozenset({
    "system.director.feed_boosted",
    "system.director.spotlighted",
    "system.director.season_set",
})


def is_narrative(type_: str) -> bool:
    """타임라인에 실을 이벤트인가 — 위 블랙리스트 근거 참고."""
    if type_.startswith(_NON_NARRATIVE_PREFIXES):
        return False
    return type_ not in _NON_NARRATIVE_TYPES


# ── 타입별 한글 한 줄 요약 ────────────────────────────────────────────

#: 타입 → 사람 문장이 담긴 payload 필드 (headline/intent/text/description 계열).
#: 엔진 사유(reason/note)를 나르는 타입은 여기 없다 — 기계 형식 검문을 거치는
#: 아래 summarize의 전용 분기가 맡는다.
_SUMMARY_FIELDS: dict[str, str] = {
    "feed.post.published": "title",
    "actor.action.performed": "intent",
    "actor.message.sent": "text",
    "actor.identity.declared": "bio",
    "actor.memory.consolidated": "summary",
    "actor.belief.formed": "statement",
    "actor.goal.advanced": "description",
    "actor.goal.achieved": "description",
    "player.comment.posted": "text",
    "player.dm.sent": "text",
    "world.incident.occurred": "description",
    "world.observation.surfaced": "observation",
    "system.director.arc_planned": "intention",
}

#: 반응 종류 → 사람 말 — 내부 표기(kind 코드)를 화면 문장에 내보내지 않는다
_REACTION_LABELS: dict[str, str] = {"like": "좋아요"}

# ── 엔진 사유(reason/note) 검문 — 기계 어휘·수치의 화면 유출 방지 ──────
# 실사고: emotion 엔진의 reason은 "player.comment.posted — gratitude 0.75
# (플레이어 p_observer_0417)" 같은 기계 형식이고, 옛 summarize가 그대로
# 화면 문장에 실었다. projector timeline._humanize_note 선례를 따른다:
# 이벤트 타입 토큰은 사람 말로 번역, 수치가 든 사유는 통째로 버린다
# (리시트는 수치를 노출하지 않는다 — 도파민 §붕괴 방어, 조작감 방지).

#: 엔진 사유의 선행 타입 토큰 → 사람 말 (projector _INTERACTION_LABELS와 같은 결)
_NOTE_TOKEN_LABELS: dict[str, str] = {
    "player.reaction.added": "좋아요",
    "player.comment.posted": "댓글",
    "player.dm.sent": "DM",
    "feed.post": "글",  # emotion 공명 사유("feed.post — envy …")의 선행 토큰
}

_MACHINE_TOKEN = re.compile(r"^[a-z_]+(\.[a-z_]+)+")

#: 마일스톤 kind → 서사 라벨 (projector _MILESTONE_LABELS와 같은 결) — note가
#: 기계 형식이거나 비었을 때의 폴백. 모르는 kind는 국면 전환의 일반 문장.
_MILESTONE_FALLBACKS: dict[str, str] = {
    "first_met": "서로를 알게 됐다",
    "stage_transition": "관계가 새로운 국면으로 넘어갔다",
    "betrayal": "믿음이 배신으로 무너졌다",
    "confession_declined": "고백은 조심스럽게 접혔다",
}

#: 뼈대 타입의 폴백 문장 — 사슬의 시작점·포스트·응답은 문장이 비어도 접히지 않는다
_SKELETON_FALLBACKS: dict[str, str] = {
    "player.comment.posted": "댓글을 남겼다",
    "player.dm.sent": "DM을 보냈다",
    "feed.post.published": "이야기를 실었다",
    "actor.message.sent": "말을 건넸다",
    "actor.action.performed": "움직임이 있었다",
}

#: 모르는 타입·빈 문장의 최후 폴백 — 항목은 남긴다(침묵 실패 회피, 구조 필드
#: type이 관찰 좌표다). 타입 라벨을 문장으로 쓰던 옛 폴백은 기계 어휘 유출이었다.
_UNKNOWN_SUMMARY = "무언가가 일어났다"


def _engine_note(payload: dict[str, Any], field: str) -> tuple[str | None, str | None]:
    """엔진이 쓴 사유 한 줄 → (살릴 문장 | None, 선행 상호작용 라벨 | None).

    타입 토큰으로 시작하면 기계 형식 통째다 — 문장으로 못 살리고 라벨만 건진다.
    수치가 든 사유도 버린다 (침묵이 노출보다 낫다 — _humanize_note 선례).
    """
    note = str(payload.get(field) or "")
    match = _MACHINE_TOKEN.match(note)
    if match is not None:
        return None, _NOTE_TOKEN_LABELS.get(match.group(0))
    if not note or re.search(r"\d", note):
        return None, None
    return note, None


def summarize(type_: str, payload: dict[str, Any]) -> str:
    """이벤트 한 건 → 타임라인 한 줄 — 언제나 사람 문장이다.

    이벤트 타입 토큰·수치·기계 사유는 화면 문장에 싣지 않는다. 원시 id
    정화(humanize_ids)는 이름 그라운딩이 필요해 호출자(timeline)의 몫이다.
    """
    if type_ == "player.reaction.added":
        label = _REACTION_LABELS.get(payload.get("kind", ""), "따뜻한 반응")
        return f"{label}로 마음을 보탰다"
    if type_ == "player.follow.changed":
        return (
            "소식을 따라가기로 했다" if payload.get("following") else "따라가기를 거뒀다"
        )
    if type_ == "actor.emotion.shifted":
        note, label = _engine_note(payload, "reason")
        if note is not None:
            return note
        return f"{label}에 마음이 움직였다" if label else "마음이 움직였다"
    if type_ == "relationship.state.changed":
        note, label = _engine_note(payload, "reason")
        if note is not None:
            return note
        return f"{label}에 마음의 거리가 달라졌다" if label else "마음의 거리가 달라졌다"
    if type_ == "relationship.milestone.reached":
        note, _ = _engine_note(payload, "note")
        if note is not None:
            return note
        return _MILESTONE_FALLBACKS.get(
            payload.get("milestone", ""), "관계가 새로운 국면으로 넘어갔다"
        )
    if type_ == "system.director.intervened":
        # 규칙 개입의 reason은 수치 보고서("침체 감지 — drama 이동평균 …")다 —
        # LLM rationale(사람 문장)만 살리고 수치 보고서는 무대 뒤 한 줄로 접는다.
        note, _ = _engine_note(payload, "reason")
        return note if note is not None else "세계가 조용히 무대를 움직였다"
    field = _SUMMARY_FIELDS.get(type_)
    if field and payload.get(field):
        return str(payload[field])
    return _SKELETON_FALLBACKS.get(type_, _UNKNOWN_SUMMARY)


# ── 표시 이름 해석 ───────────────────────────────────────────────────


def display_actor(
    actor_id: str | None,
    payload: dict[str, Any],
    *,
    names: dict[str, str],
    requester: str | None,
) -> str:
    """행위 주체의 표시 이름 — 화면 문장에 식별자를 내보내지 않는다.

    - 액터: read.actors 이름, 미상이면 '누군가' (FE 내레이터 규약과 같은 결)
    - 플레이어: 요청자 본인만 '당신', 타인은 '어느 관찰자' (관찰자 익명성)
    - 주체 없는 사건(world.*/director 개입): 세계 자신의 목소리 — '세계'
    """
    if actor_id is not None:
        return names.get(actor_id, "누군가")
    player_id = payload.get("player_id")
    if player_id is not None:
        return "당신" if requester is not None and player_id == requester else "어느 관찰자"
    return "세계"


# ── es 사슬 읽기 ─────────────────────────────────────────────────────

# 무서사 제외를 SQL로 민다 — limit(상한 노브)이 '이야기 항목' 기준으로 걸리게.
# 파라미터는 위 블랙리스트 상수에서 파생된다 (단일 출처).
_CHAIN_SQL = """
SELECT event_id, type, actor_id, tick, occurred_at, payload
FROM es.events
WHERE world_id = %s AND correlation_id = %s
  AND type NOT LIKE ALL(%s::text[]) AND type != ALL(%s::text[])
ORDER BY global_seq
LIMIT %s
"""

_NAMES_SQL = """
SELECT actor_id, name FROM read.actors
WHERE world_id = %s AND actor_id = ANY(%s)
"""

# 개입 사슬이 낳은 후속 포스트 — 판정은 loop_health의 '드라마 재생산'과 동일:
# 개입 correlation(플레이어 댓글·좋아요의 correlation_id = post_id, gateway 규약)을
# 승계한 feed.post.published가 2차 사건이다. 사슬 뿌리(원 포스트) 자신은 제외.
# 요약만 나른다(개수 + 최신 1건) — 전체 사슬은 기존 /story가 정본이다.
_DERIVED_SQL = """
SELECT count(*) OVER () AS total,
       event_id, actor_id, payload->>'title' AS title, occurred_at
FROM es.events
WHERE world_id = %s AND correlation_id = %s
  AND type = 'feed.post.published' AND event_id != %s
ORDER BY global_seq DESC
LIMIT 1
"""


class StoryReads:
    """읽기 전용 사슬 질의 — pool은 psycopg_pool.AsyncConnectionPool(또는 테스트 대역)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def timeline(
        self, world_id: str, correlation_id: str, *, player_id: str | None, limit: int
    ) -> dict[str, Any]:
        """한 correlation의 사건 연쇄를 global_seq(적재) 순의 타임라인으로."""
        excluded_likes = [f"{p}%" for p in _NON_NARRATIVE_PREFIXES]
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                _CHAIN_SQL,
                (world_id, correlation_id, excluded_likes,
                 sorted(_NON_NARRATIVE_TYPES), limit),
            )).fetchall()

        # 이름 그라운딩 대상: 행의 주체 + 문장 속 액터 참조 — es는 불변이라
        # 과거 발행분의 문장에 원시 id가 남아 있다 (읽기 경계 정화, sanitize.py)
        summaries = [summarize(type_, payload) for _, type_, _, _, _, payload in rows]
        referenced = {actor_id for _, _, actor_id, _, _, _ in rows if actor_id}
        for summary in summaries:
            referenced |= find_actor_refs(summary)
        names = await self._actor_names(world_id, referenced)
        items = [
            {
                "event_id": event_id,
                "type": type_,  # 구조 필드 — 정화는 화면 문장(actor/summary)만
                "tick": tick,
                "occurred_at": occurred_at,
                "actor": display_actor(actor_id, payload, names=names, requester=player_id),
                "summary": humanize_ids(summary, names),
            }
            for (event_id, type_, actor_id, tick, occurred_at, payload), summary
            in zip(rows, summaries, strict=True)
        ]

        # 저자성 판정 — origin이 플레이어 개입이고 그 주인이 요청자다
        # (plan/03: "이 드라마의 원작자가 나"라는 감각이 이 기능의 존재 이유)
        origin = items[0] if items else None
        started_by_you = (
            origin is not None
            and origin["type"].startswith("player.")
            and player_id is not None
            and rows[0][5].get("player_id") == player_id
        )
        return {
            "world_id": world_id,
            "correlation_id": correlation_id,
            "items": items,
            "origin": origin,
            "started_by_you": started_by_you,
        }

    async def derived_posts(self, world_id: str, correlation_id: str) -> dict[str, Any]:
        """이 사슬(correlation)이 낳은 후속 포스트 요약 — "이 대화가 낳은 이야기".

        댓글 스레드에서 2차 사건의 존재를 알리는 배지 재료다 (plan/03 실세 단계:
        "이 사건, 내가 만들었다"). 최신 1건의 제목·저자 표시 이름만 동봉한다 —
        원시 id는 사슬 화면 진입 좌표(correlation)로만 쓰이고 문장에 실리지 않는다.
        """
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                _DERIVED_SQL, (world_id, correlation_id, correlation_id)
            )).fetchone()
        if row is None:
            return {"count": 0, "latest": None}
        total, event_id, actor_id, title, occurred_at = row
        # 제목도 화면 문장이다 — 과거 발행분의 원시 id를 읽기 경계에서 정화
        referenced = ({actor_id} if actor_id else set()) | find_actor_refs(title or "")
        names = await self._actor_names(world_id, referenced)
        return {
            "count": total,
            "latest": {
                "post_id": event_id,
                "title": humanize_ids(title, names) if title else title,
                # 이름 그라운딩 — 액터는 read.actors, 주체 없는 사건은 '세계'
                "author": display_actor(actor_id, {}, names=names, requester=None),
                "occurred_at": occurred_at,
            },
        }

    async def _actor_names(self, world_id: str, actor_ids: set[str]) -> dict[str, str]:
        """read.actors 이름 해석 — 장식이다. read 미구축이 사슬 조회를 죽이면 안 된다."""
        if not actor_ids:
            return {}
        try:
            async with self._pool.connection() as conn:
                rows = await (await conn.execute(
                    _NAMES_SQL, (world_id, sorted(actor_ids))
                )).fetchall()
            return dict(rows)
        except Exception as e:
            logger.warning("read.actors 이름 해석 실패(익명 폴백): %s", e)
            return {}
