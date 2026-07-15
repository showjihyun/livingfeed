"""규칙 기반 폴백 행동 (ADR-012 §인지 루프).

LLM 실패 시 액터는 '머뭇거린' 것으로 처리되고 tick은 계속 흐른다.
성격 파라미터(needs_bias)로 개인화된 결정적 기본 행동을 만든다.
"""

from __future__ import annotations

import zlib
from typing import Any

from lf_actor.arc import Arc
from lf_actor.persona import Persona

#: 욕구 → 기본 행동 매핑 (docs/plan/06 욕구 모델의 최소 투영)
_NEED_ACTIONS = {
    "achievement": ("work", "밀린 일을 붙잡는다"),
    "belonging": ("observe", "주변 사람들의 근황을 살핀다"),
    "security": ("rest", "하루를 정리하며 숨을 고른다"),
}


#: 규칙 답장 템플릿 — 욕구 편향별 톤 (결정적 선택)
_REPLY_TONES = {
    "achievement": (
        "고마워요. 요즘 일에 파묻혀 있었는데, 이런 말은 오래 남네요.",
        "그 말, 기록해 둘게요. 지금 하는 일이 끝나면 꼭 다시 얘기해요.",
        "인정받는 기분이라 낯설지만… 나쁘지 않네요. 고마워요.",
    ),
    "belonging": (
        "이렇게 말 걸어주는 사람이 있다는 게 큰 힘이 돼요. 정말로요.",
        "읽고 한참 있었어요. 답이 늦었죠 — 진심이 느껴져서요.",
        "고마워요. 요즘 사람들 속에서도 혼자라는 기분이었거든요.",
    ),
    "security": (
        "고마워요. 조금은 마음이 놓이네요.",
        "그 말 덕분에 오늘 밤은 좀 잘 수 있을 것 같아요.",
        "조심스럽지만… 믿어볼게요. 고마워요.",
    ),
}
_REPLY_DEFAULT = (
    "고마워요. 생각할 게 많은 요즘이라, 그 말이 꽤 오래 남을 것 같아요.",
    "…솔직히 조금 놀랐어요. 이렇게 봐주는 사람이 있구나 싶어서.",
    "답이 짧아서 미안해요. 대신 진심이에요 — 고마워요.",
)


def fallback_reply(persona: Persona, incoming_text: str) -> str:
    """LLM 없이도 대화가 죽지 않는 결정적 규칙 답장 (ADR-012 폴백).

    같은 (페르소나, 수신 텍스트) → 같은 답장. 표현 품질은 LLM(converse)의
    몫이고, 이것은 '세계가 반드시 응답한다'는 보증이다.
    """
    if persona.needs_bias:
        need = max(sorted(persona.needs_bias), key=lambda k: persona.needs_bias[k])
        tones = _REPLY_TONES.get(need, _REPLY_DEFAULT)
    else:
        tones = _REPLY_DEFAULT
    return tones[zlib.crc32(f"{persona.id}:{incoming_text}".encode()) % len(tones)]


def fallback_action(persona: Persona, tick: int, trace_id: str) -> dict[str, Any]:
    """actor.action.performed payload 형태의 결정적 규칙 행동."""
    if persona.needs_bias:
        # 가장 강한 욕구 기준, 동률은 이름 순 — 결정적
        need = max(sorted(persona.needs_bias), key=lambda k: persona.needs_bias[k])
        kind, base_intent = _NEED_ACTIONS.get(need, ("rest", "잠시 멈춰 생각한다"))
    else:
        kind, base_intent = "rest", "잠시 멈춰 생각한다"
    # tick별 미세 변화 (결정적)
    variant = zlib.crc32(f"{persona.id}:{tick}".encode()) % 3
    suffix = ["", " — 습관처럼", " — 마음이 다른 데 가 있다"][variant]
    return {
        "action_kind": kind,
        "intent": f"{persona.name}, {base_intent}{suffix}",
        "target_actor_id": None,
        "location_id": None,
        "params": {"fallback": True},
        "decision_trace": {"trace_id": trace_id, "tier": "cold_rule"},
    }


#: Cold 일과 패턴 — 잠든 기간의 생활을 요약하는 서술 (ADR-012 §Cold 통계적 일괄 처리).
#: 순간의 머뭇거림(fallback_action)과 달리 기간의 결이 담긴다. 최강 욕구가 기조,
#: 두 번째 욕구가 틈틈이 스민다.
_ROUTINE_PATTERNS: dict[str, tuple[str, tuple[str, ...]]] = {
    "achievement": ("work", (
        "밀린 일을 차례로 쳐내며 조용히 보냈다",
        "책상 앞을 지키며 제 몫의 일을 쌓았다",
        "눈에 띄지 않게, 그러나 꾸준히 하던 일을 밀고 나갔다",
    )),
    "belonging": ("observe", (
        "사람들의 근황을 살피며 조용히 지냈다",
        "익숙한 얼굴들 곁을 맴돌며 시간을 보냈다",
        "연락할까 말까 망설이며 한동안을 흘려보냈다",
    )),
    "security": ("rest", (
        "생활의 리듬을 지키며 조용히 지냈다",
        "무리하지 않는 선에서 하루하루를 정리했다",
        "익숙한 일과를 반복하며 마음을 다독였다",
    )),
}

_ROUTINE_SEASONING = {
    "achievement": "틈틈이 밀린 일도 들여다봤다",
    "belonging": "틈틈이 아는 사람들 소식도 살폈다",
    "security": "틈틈이 생활을 정돈하기도 했다",
}


def routine_action(
    persona: Persona, tick: int, trace_id: str, *, arc: Arc | None = None
) -> dict[str, Any]:
    """Cold 일과 행동 — 잠든 기간의 생활 요약 (ADR-012 §Cold '일과 이벤트 생성').

    due tick(100 케이던스)마다 하나 — 기간을 서술하는 일과라 스팸이 아니다.
    최강 욕구가 일과의 기조를, 두 번째 욕구가 양념을, tick 회전이 표현을 고른다.
    아크(있으면)가 마지막 결을 준다 — 잠든 삶도 방향이 있다 (ADR-013/plan-08).
    결정적 — 같은 (페르소나, tick, 아크)면 같은 일과다. 목표 응고(record_action)를
    타고 잠든 삶도 천천히 전진한다.
    """
    ranked = sorted(persona.needs_bias or {"security": 1.0}, key=lambda k: (
        -(persona.needs_bias or {"security": 1.0})[k], k,
    ))
    primary = ranked[0] if ranked[0] in _ROUTINE_PATTERNS else "security"
    kind, phrases = _ROUTINE_PATTERNS[primary]
    phrase = phrases[zlib.crc32(f"{persona.id}:routine:{tick}".encode()) % len(phrases)]
    intent = f"{persona.name}, {phrase}"
    secondary = next((n for n in ranked[1:] if n in _ROUTINE_SEASONING), None)
    if secondary is not None:
        intent += f". {_ROUTINE_SEASONING[secondary]}"
    if arc is not None:
        intent += f". 마음 한켠엔 늘 같은 방향이 있다 — {arc.intention}"
    return {
        "action_kind": kind,
        "intent": intent[:500],
        "target_actor_id": None,
        "location_id": None,
        "params": {"fallback": True, "routine": True},
        "decision_trace": {"trace_id": trace_id, "tier": "cold_rule"},
    }
