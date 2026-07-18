"""관계 갱신·서술의 순수 로직 (ADR-016 §갱신 규칙).

전부 결정적 순수 함수 — 같은 (상태, 사건, 파라미터) → 같은 결과.
LLM 호출·I/O 금지 (ADR-015와 동일 논거). 저장·이벤트 적재는 어댑터의 몫.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import yaml

from lf_relationship.model import DIMENSIONS, RelationshipState, clamp_dimension

Direction = str  # "outgoing" | "incoming"


@cache
def default_params() -> dict[str, Any]:
    """params.yaml — 델타 튜닝의 단일 원천 (ADR-016 완화책)."""
    text = (files("lf_relationship") / "params.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@dataclass(frozen=True)
class UpdateResult:
    state: RelationshipState
    #: 발행 임계를 넘었나 — True면 호출자가 relationship.state.changed를 적재하고
    #: consume_pending()으로 누적분을 비운다
    publish: bool
    #: 사람이 읽는 변화 원인 한 줄 — 아래 reason 계약 참조
    reason: str


# ── reason 계약 — 사람 문장 (발원지 정화) ─────────────────────────────────
# reason은 리시트(projector 타임라인)·이야기 사슬(feed-api story)·LLM 컨텍스트에
# 그대로 실리는 화면 문장이다. 계약: 이벤트 타입 토큰·방향 표기(incoming 등)·
# 수치·원시 id 금지, 결정적(같은 입력 → 같은 문장 — 리플레이 원칙). 판정용
# 구조 정보(차원·델타·stage·salience)는 payload의 구조 필드가 이미 나른다 —
# reason은 사람 몫, 판정은 구조 필드 몫 (이원화).

#: (사건, 방향) → 원인 문장 — params interaction_effects의 전 조합을 덮는다.
#: 리시트 본문이 "{reason} — {정성 문장}."으로 조립되므로 문장 하나로 완결돼야 한다.
_INTERACTION_REASONS: dict[tuple[str, str], str] = {
    ("player.dm.sent", "incoming"): "DM 한 통을 받았다",
    ("player.comment.posted", "incoming"): "댓글 한 마디를 받았다",
    ("player.reaction.added", "incoming"): "좋아요로 마음을 건네받았다",
    # 액터끼리 오간 말 (규칙 1c) — 플레이어 댓글("받았다")과 어휘를 살짝 달리해
    # 리시트에서 출처의 결이 구분된다 (둘 다 사람 문장, 기계 토큰 없음)
    ("actor.message.sent", "outgoing"): "댓글로 말을 건넸다",
    ("actor.message.sent", "incoming"): "댓글 한 마디가 건너왔다",
    ("action.speak", "outgoing"): "먼저 말을 건넸다",
    ("action.speak", "incoming"): "말을 건네받았다",
    ("action.help", "outgoing"): "손을 내밀어 도왔다",
    ("action.help", "incoming"): "도움의 손길을 받았다",
    ("action.confront", "outgoing"): "따져 물었다",
    ("action.confront", "incoming"): "따가운 말을 마주했다",
    ("action.confess", "outgoing"): "마음을 고백했다",
    ("action.confess", "incoming"): "고백을 받았다",
    ("action.sever", "outgoing"): "관계를 끊어냈다",
    ("action.sever", "incoming"): "관계가 끊겼다",
}

#: 미지 사건의 방향별 폴백 — params에 새 사건이 늘어도 문장은 사람 말로 남는다
_FALLBACK_INTERACTION_REASONS: dict[str, str] = {
    "incoming": "마음을 건네받았다",
    "outgoing": "마음을 건넸다",
}

#: 감정 코드 → 한국어 어휘(주격 조사 포함) — emotion_consolidation의 전 코드를
#: 덮는다. projector 리시트·feed-api 이야기 사슬의 한국어 감정 어휘와 같은 결.
_EMOTION_WORDS: dict[str, str] = {
    "gratitude": "고마움이",
    "joy": "기쁨이",
    "anger": "노여움이",
    "distress": "괴로움이",
}

_FALLBACK_EMOTION_WORD = "일렁인 마음이"

#: '깊이'가 붙는 강도 문턱 — 수치는 문장에 싣지 않고 세기로만 남긴다 (남발 금지)
_STRONG_INTENSITY = 0.7


def _interaction_reason(source_kind: str, direction: Direction) -> str:
    fallback = _FALLBACK_INTERACTION_REASONS.get(direction, "마음이 오갔다")
    return _INTERACTION_REASONS.get((source_kind, direction), fallback)


def _consolidation_reason(emotion_type: str, intensity: float) -> str:
    word = _EMOTION_WORDS.get(emotion_type, _FALLBACK_EMOTION_WORD)
    depth = "깊이 " if intensity >= _STRONG_INTENSITY else ""
    return f"그 사람과의 사이에 {word} {depth}쌓였다"


def _apply_deltas(
    state: RelationshipState, deltas: dict[str, float], *, salience_delta: float,
    reason: str, params: dict[str, Any],
) -> UpdateResult:
    dimensions = dict(state.dimensions)
    pending = dict(state.pending)
    for name, delta in deltas.items():
        before = dimensions[name]
        dimensions[name] = clamp_dimension(name, before + delta)
        pending[name] += dimensions[name] - before  # 클램프 반영된 실효 델타만 누적
    new_state = RelationshipState(
        dimensions=dimensions,
        stage=state.stage,
        salience=min(1.0, max(0.0, state.salience + salience_delta)),
        pending=pending,
    )
    publish = new_state.pending_l1() >= params["publish_threshold"]
    return UpdateResult(state=new_state, publish=publish, reason=reason)


def apply_interaction(
    state: RelationshipState,
    source_kind: str,
    direction: Direction,
    *,
    params: dict[str, Any] | None = None,
) -> UpdateResult:
    """상호작용 효과 (갱신 규칙 1). source_kind는 params의 interaction_effects 키다."""
    params = params or default_params()
    effect = params["interaction_effects"].get(source_kind, {}).get(direction)
    if not effect:
        return UpdateResult(state=state, publish=False, reason="")
    deltas = {k: v for k, v in effect.items() if k in DIMENSIONS}
    return _apply_deltas(
        state, deltas,
        salience_delta=effect.get("salience", 0.0),
        reason=_interaction_reason(source_kind, direction),
        params=params,
    )


def consolidate_emotion(
    state: RelationshipState,
    emotion_type: str,
    intensity: float,
    *,
    params: dict[str, Any] | None = None,
) -> UpdateResult:
    """감정 응고 (갱신 규칙 2) — 대상 있는 감정이 관계 차원으로 스며든다.

    반복 anger → resentment 누적이 여기서 일어난다 (독립 축 — 화해 후에도 앙금).
    """
    params = params or default_params()
    coefficients = params["emotion_consolidation"].get(emotion_type)
    if not coefficients:
        return UpdateResult(state=state, publish=False, reason="")
    deltas = {k: v * intensity for k, v in coefficients.items() if k in DIMENSIONS}
    return _apply_deltas(
        state, deltas,
        salience_delta=0.02 * intensity,  # 감정이 일었던 상대는 삶에서 비중이 는다
        reason=_consolidation_reason(emotion_type, intensity),
        params=params,
    )


def consolidate_insight(
    state: RelationshipState, confidence: float, *, params: dict[str, Any] | None = None
) -> UpdateResult:
    """인물 통찰 응고 — 누군가에 대해 굳어진 생각은 그 사람의 비중을 늘린다 (ADR-016).

    감정 응고(규칙 2)의 자매: 통찰은 차원(신뢰·원한)을 직접 바꾸지 않는다 —
    생각만으로 마음이 바뀌진 않는다. 대신 salience가 자란다: 그 사람이
    내 삶에서 차지하는 자리. 발행 대상이 아니다 (조용한 내면 변화).
    """
    params = params or default_params()
    delta = float(params.get("insight_salience", 0.0)) * confidence
    if delta <= 0.0:
        return UpdateResult(state=state, publish=False, reason="")
    new_state = RelationshipState(
        dimensions=state.dimensions,
        stage=state.stage,
        salience=min(1.0, state.salience + delta),
        pending=state.pending,
    )
    return UpdateResult(
        state=new_state, publish=False, reason="그 사람 생각이 마음속 자리를 넓혔다"
    )


def decay(
    state: RelationshipState, ticks: int, *, params: dict[str, Any] | None = None
) -> RelationshipState:
    """시간 감쇠 (갱신 규칙 4) — 상호작용 없으면 intimacy·salience가 식는다.

    resentment는 매우 느리게 — 잊히지만 사라지지 않는다. 감쇠분도 pending에
    누적되어, 오래 식은 관계는 결국 state.changed로 세계에 기록된다.
    """
    if ticks <= 0:
        return state
    params = params or default_params()
    rates = params["decay_per_tick"]
    dimensions = dict(state.dimensions)
    pending = dict(state.pending)
    for name, rate in rates.items():
        if name not in DIMENSIONS:
            continue
        before = dimensions[name]
        dimensions[name] = clamp_dimension(name, before - rate * ticks)
        pending[name] += dimensions[name] - before
    salience = max(0.0, state.salience - rates.get("salience", 0.0) * ticks)
    return RelationshipState(
        dimensions=dimensions, stage=state.stage, salience=salience, pending=pending
    )


def consume_pending(state: RelationshipState) -> tuple[RelationshipState, dict[str, float]]:
    """발행 시점 — 누적 델타를 돌려주고 pending을 비운다."""
    deltas = {k: round(v, 4) for k, v in state.pending.items()}
    cleared = RelationshipState(
        dimensions=state.dimensions,
        stage=state.stage,
        salience=state.salience,
        pending={k: 0.0 for k in DIMENSIONS},
    )
    return cleared, deltas


def transition_stage(state: RelationshipState, stage: str) -> RelationshipState:
    """stage 전이 — 수치가 아니라 이벤트(행동·마일스톤)가 만든다 (ADR-016)."""
    return RelationshipState(
        dimensions=state.dimensions, stage=stage,
        salience=state.salience, pending=state.pending,
    )


@dataclass(frozen=True)
class StageTransition:
    """상위 전이 행동의 판정 결과 — stage가 None인 방향은 유지된다 (거절 등)."""

    actor_stage: str | None  # 행위자→대상 엣지의 새 stage
    target_stage: str | None  # 대상→행위자 엣지의 새 stage
    milestone: str
    note: str


#: stage 전이를 만드는 행동들 — LLM decide만 고른다 (action_kind는 스키마상
#: 자유 패턴). 규칙 폴백은 절대 안 골라 dev 결정성은 불변이다 (ADR-016).
STAGE_ACTION_KINDS = frozenset({"confess", "sever"})

#: confess 수락 임계 기본값 — params.yaml stage_transitions가 재정의한다 (튜닝 노브)
_CONFESS_TRUST = 0.2
_CONFESS_RESENTMENT = 0.3


def stage_after_action(
    action_kind: str,
    actor_edge: RelationshipState,
    target_edge: RelationshipState,
    *,
    params: dict[str, Any] | None = None,
) -> StageTransition | None:
    """상위 전이 행동(confess/sever) → stage 전이 판정 (ADR-016).

    stage 전이는 수치가 아니라 이벤트가 만든다 — 차원은 전이의 *조건*일 뿐이다.
    confess(고백)의 답은 대상→행위자 엣지가 정한다: 조건이 맞으면 양쪽이
    romantic이 되고, 아니면 stage 변화 없는 거절 마일스톤이다 — 거절도 세계에
    남는 사건. sever(절교)는 조건 없이 양쪽을 stranger로 되돌린다 — salience는
    손대지 않는다 (끊었어도 마음의 흔적은 남는다). 이미 그 관계라면 None:
    재고백·재절교는 마일스톤이 아니다. 결정적·순수 — I/O 없음.
    """
    params = params or default_params()
    knobs = params.get("stage_transitions", {})
    if action_kind == "confess":
        if actor_edge.stage == "romantic" and target_edge.stage == "romantic":
            return None
        accepted = (
            target_edge.dimensions["trust"]
            >= float(knobs.get("confess_accept_trust", _CONFESS_TRUST))
            and target_edge.dimensions["resentment"]
            < float(knobs.get("confess_reject_resentment", _CONFESS_RESENTMENT))
        )
        if accepted:
            return StageTransition(
                actor_stage="romantic", target_stage="romantic",
                milestone="confession_accepted",
                note="고백이 받아들여졌다 — 연인이 되었다",
            )
        return StageTransition(
            actor_stage=None, target_stage=None,
            milestone="confession_declined",
            note="고백했지만 받아들여지지 않았다 — 마음이 아직 닿지 않는다",
        )
    if action_kind == "sever":
        if actor_edge.stage == "stranger" and target_edge.stage == "stranger":
            return None
        return StageTransition(
            actor_stage="stranger", target_stage="stranger",
            milestone="severed",
            note="관계를 끊어냈다 — 남남으로 돌아갔지만 흔적은 남는다",
        )
    return None


#: 결 판정 임계 — reflection의 규칙 신념(derive_beliefs)과 감각을 맞춘다:
#: supporter(trust 0.25 · intimacy 0.15) / threat(resentment 0.3)
_TRUST_CLOSE = 0.25
_INTIMACY_CLOSE = 0.15
_RESENTMENT_GRUDGE = 0.3
#: respect·attraction의 결 임계 — trust와 같은 0.25 (한 축이 뚜렷해지는 지점)
_RESPECT_DEFER = 0.25
_ATTRACTION_DRAWN = 0.25


@dataclass(frozen=True)
class _Texture:
    """결 어휘 한 조각 — 단독 문장과 조합용 절 형태 (조합 규칙은 _edge_texture).

    tension이 True면 긴장 결(앙금·불신) — 조합 시 항상 뒷절에 놓여 온기
    앞절과 "…지만"으로 대비된다 (기존 애증 문장 "믿고 가까운 사이지만,
    앙금도 남아 있다"의 관례를 일반화). lead는 온기 결의 앞절 어간
    ("지만"/"고" 앞에 붙는 형태), tail은 뒷절 문장.
    """

    tension: bool
    solo: str
    lead: str
    tail: str


_GRUDGE = _Texture(
    tension=True,
    solo="앙금이 남아 있다 — 쌓인 것이 쉽게 사라지지 않는다",
    lead="", tail="앙금도 남아 있다",
)
_DISTRUST = _Texture(
    tension=True, solo="선뜻 믿기 어려운 상대다",
    lead="", tail="선뜻 믿기 어려운 상대다",
)
_CONTEMPT = _Texture(
    tension=True, solo="어딘가 얕보게 되는 상대다",
    lead="", tail="어딘가 얕보게 되는 상대다",
)
_DRAWN = _Texture(
    tension=False, solo="자꾸 눈이 가는 사람이다",
    lead="자꾸 눈이 가는 사람이", tail="자꾸 눈이 가는 사람이다",
)
_DEFERENT = _Texture(
    tension=False, solo="한 수 접어주게 되는 상대다",
    lead="한 수 접어주게 되는 상대", tail="한 수 접어주게 되는 상대다",
)
_CLOSE = _Texture(
    tension=False, solo="믿고 가까운 사이다",
    lead="믿고 가까운 사이", tail="믿고 가까운 사이다",
)


def _edge_texture(dims: dict[str, float]) -> str:
    """엣지 하나의 결 — 수치가 아니라 감각의 언어로 (ADR-009 Relationship(3)).

    애증은 공존한다 — 차원들이 독립 축이라(ADR-016) 임계를 넘은 결은 함께
    말한다. 화해했어도 앙금은 남는다. 다만 최대 2개까지 — 나열이 길어지면
    감각이 아니라 목록이 된다. 선별 우선순위는 긴장(앙금·불신·경멸) > 끌림 >
    존중 > 친밀·신뢰: 긴장이 서사적으로 우선이다. 두 결의 조합은 온기
    앞절 + 긴장 뒷절을 "…지만"으로(대비), 온기끼리는 "…고"로(병렬) 잇는다.
    """
    candidates: list[_Texture] = []
    if dims["resentment"] >= _RESENTMENT_GRUDGE:
        candidates.append(_GRUDGE)
    elif dims["trust"] <= -_TRUST_CLOSE:  # 앙금이 있으면 불신은 그 안에 접힌다
        candidates.append(_DISTRUST)
    elif dims["respect"] <= -_RESPECT_DEFER:  # 경멸 — 더 뜨거운 긴장(앙금·불신)에 접힌다
        candidates.append(_CONTEMPT)
    if dims["attraction"] >= _ATTRACTION_DRAWN:
        candidates.append(_DRAWN)
    if dims["respect"] >= _RESPECT_DEFER:
        candidates.append(_DEFERENT)
    if dims["trust"] >= _TRUST_CLOSE and dims["intimacy"] >= _INTIMACY_CLOSE:
        candidates.append(_CLOSE)
    picked = candidates[:2]
    if not picked:
        return "아직 마음의 결이 뚜렷하지 않은 사이다"
    if len(picked) == 1:
        return picked[0].solo
    first, second = picked
    if first.tension:  # 긴장+온기 — 온기가 앞, 긴장이 뒤 (대비의 방향이 서사다)
        return f"{second.lead}지만, {first.tail}"
    return f"{first.lead}고, {second.tail}"


#: stage 라벨 — 기본 단계(stranger/acquaintance)는 표기 생략: 이름이 붙기 전의
#: 관계에 이름을 달면 소음이다. 키는 model.py STAGES의 부분집합 (단일 원천).
_STAGE_LABELS = {
    "friend": "친구",
    "close_friend": "가까운 친구",
    "romantic": "연인",
    "family": "가족",
    "mentor": "스승과 제자 사이",
    "rival": "라이벌",
    "enemy": "적",
}


def describe_edges(
    edges: dict[str, RelationshipState],
    name_map: dict[str, str] | None = None,
    *,
    limit: int = 3,
) -> str | None:
    """관계 요약 — salience 상위 엣지를 사람이 읽는 한 줄씩으로 (ADR-009 §3).

    decide 컨텍스트의 Relationship(3) 섹션 재료다: 원한이 쌓인 상대에게
    태연히 말을 걸지 않으려면, 결정 앞에 관계의 온도가 놓여야 한다.
    이름은 name_map으로 그라운딩(없으면 id — 플레이어 등 명부 밖 상대).
    기본 아닌 stage는 이름 뒤에 관계 라벨을 붙인다 — 라이벌의 앙금과
    낯선 이의 앙금은 다른 온도다. 결정적·순수: salience 내림차순,
    동률은 id 순. 엣지가 없으면 None (섹션 생략 규약 — 빈 관계는 소음이다).
    """
    if not edges:
        return None
    names = name_map or {}
    ranked = sorted(edges, key=lambda other: (-edges[other].salience, other))[:limit]

    def line(other: str) -> str:
        name = names.get(other, other)
        if label := _STAGE_LABELS.get(edges[other].stage):
            name = f"{name}({label})"
        return f"- {name}: {_edge_texture(edges[other].dimensions)}"

    return "\n".join(line(other) for other in ranked)
