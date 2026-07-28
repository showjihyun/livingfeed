"""L2 규칙 재실행 — 세계에서 LLM을 뺀 부분은 완전히 결정적이다 (ADR-021 §4).

규칙 경로(provenance=derived)는 순수 함수다: 같은 입력이면 같은 출력이고, 그
'같음'은 비트 단위다. 그래서 이 등급만은 L3가 못 주는 것을 준다 — **다시 돌려서
같은 답이 나오는지 실제로 확인할 수 있다.**

무엇을 확인하는가: 기록된 규칙 이벤트의 payload를, 같은 규칙에 같은 입력을 먹여
다시 만든 payload와 대조한다. 어긋나면 규칙이 조용히 바뀐 것이다 — 리팩터링이
행동을 바꿨거나, 튜닝 값이 움직였거나, 결정적이라 믿었던 것이 아니었거나.

## persona는 이벤트가 아니다

`fallback_action(persona, tick, trace_id)`의 persona는 저작물이다 (YAML 또는
스튜디오, provenance=authored). 이벤트 로그에서 복원되지 않으므로 **호출자가
그 세계가 쓰던 것과 같은 원천을 줘야 한다.** 이것은 결함이 아니라 계약이다:
저작물이 바뀌면 규칙의 출력도 바뀌는 게 맞고, L2가 잡아야 할 것은 '저작물이
같은데 출력이 달라진' 경우다.

## 여기 없는 규칙들

`derive_beliefs`는 감정·관계 **상태**에서 파생되는데 그 상태는 Redis에 살고
이벤트가 아니다. 상태를 그 tick까지 되감지 않으면 재실행할 수 없으므로, 지금
단계의 L2는 (persona, tick)만으로 닫히는 규칙에 한정한다. 닫히지 않는 규칙은
UnsupportedRule로 **거절한다** — 검증하지 못한 것을 통과로 세면 L2의 보증이
그 순간 거짓이 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from lf_eventstore.tiers import ReplayTier, assert_verifiable

from lf_actor.arc import Arc
from lf_actor.persona import Persona
from lf_actor.rules import fallback_action, fallback_follow_up, routine_action


class UnsupportedRule(Exception):
    """이 rule_id는 (persona, tick)만으로 재실행되지 않는다 — 통과로 세지 않는다."""


class _RuleFn(Protocol):
    def __call__(
        self, persona: Persona, tick: int, trace_id: str, **kwargs: Any
    ) -> dict[str, Any]: ...


def _follow_up(persona: Persona, tick: int, trace_id: str, **kwargs: Any) -> dict[str, Any]:
    """여운 후속 — fragment(그 대화의 한 조각)가 payload 안에 인용으로 남아 있다.

    원문 재구성이 아니라 호출자가 준 fragment로 대조한다: 인용문을 payload에서
    거꾸로 파싱하면 규칙이 바뀌었을 때 파서도 함께 틀려 대조가 무의미해진다.
    """
    fragment = kwargs.get("fragment")
    if fragment is None:
        raise UnsupportedRule(
            "actor.rules:fallback_follow_up 재실행에는 fragment가 필요하다 —"
            " 여운(Resonance)의 원 대화 조각은 이벤트가 아니라 Redis 상태다"
        )
    return fallback_follow_up(persona, tick, trace_id, fragment=str(fragment))


def _routine(persona: Persona, tick: int, trace_id: str, **kwargs: Any) -> dict[str, Any]:
    arc = kwargs.get("arc")
    return routine_action(persona, tick, trace_id, arc=arc if isinstance(arc, Arc) else None)


#: rule_id → 재실행 함수. provenance.rule_id가 규칙의 이름이자 진입점이다
#: (ADR-021 §1 "rule_id가 L2 규칙 재실행의 진입점이다").
RULES: dict[str, _RuleFn] = {
    "actor.rules:fallback_action": lambda persona, tick, trace_id, **_: fallback_action(
        persona, tick, trace_id
    ),
    "actor.rules:routine_action": _routine,
    "actor.rules:fallback_follow_up": _follow_up,
}


@dataclass(frozen=True)
class RuleVerdict:
    """재실행 대조의 결과."""

    rule_id: str
    matches: bool
    #: 어긋난 payload 키들 — 무엇이 달라졌는지가 곧 무엇이 바뀌었는지다
    differing_keys: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.matches


def rule_id_of(envelope: dict[str, Any]) -> str | None:
    """이 이벤트가 규칙 파생이면 그 규칙 이름 — 아니면 None."""
    provenance = envelope.get("provenance") or {}
    return provenance.get("rule_id") if provenance.get("kind") == "derived" else None


def verify_rule_event(
    envelope: dict[str, Any], persona: Persona, **inputs: Any
) -> RuleVerdict:
    """규칙 이벤트를 재실행해 비트 단위로 대조한다 (ADR-021 §4 L2).

    envelope은 적재된 봉투 그대로다 — provenance.rule_id가 규칙을, tick과
    payload.decision_trace.trace_id가 나머지 입력을 준다. persona와 그 외
    상태 입력(arc, fragment)은 호출자가 그 세계의 원천에서 가져와 넘긴다.
    """
    assert_verifiable(ReplayTier.RULE_REEXECUTION)

    rule_id = rule_id_of(envelope)
    if rule_id is None:
        raise UnsupportedRule(
            f"규칙 파생 이벤트가 아니다 (provenance={envelope.get('provenance')}) —"
            " L2는 derived만 다룬다"
        )
    rule = RULES.get(rule_id)
    if rule is None:
        raise UnsupportedRule(
            f"{rule_id!r}는 (persona, tick)만으로 닫히지 않거나 아직 등록되지 않았다 —"
            " 재실행할 수 없는 것을 통과로 세지 않는다"
        )

    recorded = envelope["payload"]
    trace_id = (recorded.get("decision_trace") or {}).get("trace_id", "")
    rebuilt = rule(persona, envelope["tick"], trace_id, **inputs)

    differing = tuple(
        sorted(
            key
            for key in set(recorded) | set(rebuilt)
            if recorded.get(key) != rebuilt.get(key)
        )
    )
    return RuleVerdict(rule_id=rule_id, matches=not differing, differing_keys=differing)
