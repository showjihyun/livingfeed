"""규칙 기반 폴백 행동 (ADR-012 §인지 루프).

LLM 실패 시 액터는 '머뭇거린' 것으로 처리되고 tick은 계속 흐른다.
성격 파라미터(needs_bias)로 개인화된 결정적 기본 행동을 만든다.
"""

from __future__ import annotations

import zlib
from typing import Any

from lf_actor.persona import Persona

#: 욕구 → 기본 행동 매핑 (docs/plan/06 욕구 모델의 최소 투영)
_NEED_ACTIONS = {
    "achievement": ("work", "밀린 일을 붙잡는다"),
    "belonging": ("observe", "주변 사람들의 근황을 살핀다"),
    "security": ("rest", "하루를 정리하며 숨을 고른다"),
}


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
