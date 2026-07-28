"""재현 등급 — 무엇을 재현할 수 있고 무엇은 못 하는가 (ADR-021 §4).

"deterministic replay"는 한 단어로 두 가지를 뜻하고, 섞어 쓰면 우리가 지킬 수
없는 보증을 하게 된다. 그래서 다섯 등급으로 나누고 각각의 보증을 **데이터로**
못박는다 — 문서의 문장은 잊히지만 `assert_verifiable()`은 잊히지 않는다.

가장 중요한 것은 L3(LLM 재실행)를 **보증하지 않는다고 선언하는 것**이다.
temperature=0에 모델 버전을 고정해도 비트 단위 재현은 불가능하다: 부동소수점
누적 순서의 비결정성, 배치 크기에 따른 커널 선택 차이, 프로바이더 측 모델의
조용한 갱신. 로컬 모델도 추론 엔진·하드웨어가 바뀌면 마찬가지다.

**부분적 성공이 실패보다 나쁘다.** 재현되는 것처럼 보이다 조용히 깨지는 보증은
그것을 신뢰한 실험 결과 전체를 오염시킨다. 그래서 L3를 검증하려는 시도는
'실패'가 아니라 UnverifiableTier로 거절된다 — 초록불이 뜰 길 자체를 막는다.

반사실 실험의 정식 도구는 L3가 아니라 L4(분기)다. 같은 프롬프트를 다시
불러 같은 답을 받으려 애쓰는 대신, 역사를 갈라 개입만 바꿔 두 갈래를 돌린다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReplayTier(StrEnum):
    """재현의 다섯 등급 — 값이 곧 ADR-021 §4 표의 행 이름이다."""

    PLAYBACK = "L0"
    REASSEMBLY = "L1"
    RULE_REEXECUTION = "L2"
    LLM_REEXECUTION = "L3"
    FORK = "L4"


class UnverifiableTier(Exception):
    """보증하지 않는 등급을 검증하려 했다 (ADR-021 §4).

    오류로 만드는 것이 요점이다: '검증했는데 통과'와 '검증할 수 없다'가 같은
    초록불로 보이면, 재현되지 않는 것을 재현됐다고 읽게 된다.
    """


@dataclass(frozen=True)
class TierGuarantee:
    """한 등급이 약속하는 것과 그 약속을 지키는 곳."""

    tier: ReplayTier
    name: str
    #: 이 등급의 대조가 의미를 갖는가. False면 검증 자체가 성립하지 않는다.
    verifiable: bool
    summary: str
    #: 이 등급을 실제로 집행하는 지점 — 없으면(L3) 그런 곳이 있어선 안 된다는 뜻
    entry_point: str | None


GUARANTEES: dict[ReplayTier, TierGuarantee] = {
    ReplayTier.PLAYBACK: TierGuarantee(
        tier=ReplayTier.PLAYBACK,
        name="재생 (playback)",
        verifiable=True,
        summary=(
            "기록된 이벤트를 순서대로 재적용해 상태를 복원한다. 이벤트가 불변이고"
            " 프로젝션이 멱등이므로 언제 몇 번을 돌려도 같은 상태에 닿는다."
        ),
        entry_point="lf_projector.replay.replay_into",
    ),
    ReplayTier.REASSEMBLY: TierGuarantee(
        tier=ReplayTier.REASSEMBLY,
        name="재조립 (re-assembly)",
        verifiable=True,
        summary=(
            "결정 시점의 ContextBundle을 같은 입력으로 다시 조립해 bundle_digest를"
            " 대조한다. 조립이 순수 함수라 성립한다 — LLM 출력이 재현 불가능해도"
            " **입력은 재현 가능하다**는 것이 연구용 관측성의 핵심 자산이다."
        ),
        entry_point="lf_actor.context.verify_digest",
    ),
    ReplayTier.RULE_REEXECUTION: TierGuarantee(
        tier=ReplayTier.RULE_REEXECUTION,
        name="규칙 재실행",
        verifiable=True,
        summary=(
            "규칙 경로(provenance=derived)를 같은 입력으로 다시 실행해 비트 단위로"
            " 대조한다. 세계에서 LLM을 빼면 남는 것이 전부 이 등급이며, 그 부분은"
            " 완전히 결정적이다."
        ),
        entry_point="lf_actor.replay_rules.verify_rule_event",
    ),
    ReplayTier.LLM_REEXECUTION: TierGuarantee(
        tier=ReplayTier.LLM_REEXECUTION,
        name="LLM 재실행",
        verifiable=False,
        summary=(
            "같은 프롬프트로 LLM을 다시 불러 같은 출력을 받는 것 — **보증하지"
            " 않는다**. temperature=0과 모델 버전 고정으로도 부동소수점 누적 순서,"
            " 배치 크기별 커널 선택, 프로바이더 측 모델의 조용한 갱신을 막을 수"
            " 없다. 재현이 필요하면 L1(입력 재현)과 L4(분기)를 쓴다."
        ),
        entry_point=None,
    ),
    ReplayTier.FORK: TierGuarantee(
        tier=ReplayTier.FORK,
        name="분기 (fork)",
        verifiable=True,
        summary=(
            "분기점까지의 역사를 새 world_id로 복사해 개입만 바꿔 두 갈래를 돌린다."
            " 반사실 실험의 정식 도구 — 분기점까지는 L0 보증이 그대로 서고, 그"
            " 이후의 발산 자체가 측정 대상이 된다."
        ),
        entry_point="lf_eventstore.fork.fork_world",
    ),
}


def guarantee(tier: ReplayTier) -> TierGuarantee:
    return GUARANTEES[tier]


def assert_verifiable(tier: ReplayTier) -> TierGuarantee:
    """이 등급을 검증해도 되는가 — 아니면 UnverifiableTier.

    검증기의 첫 줄에 둔다. L3에 대해 '통과'를 반환할 수 있는 코드가 생기는
    순간, 재현되지 않는 것이 재현됐다고 읽히기 시작한다.
    """
    spec = GUARANTEES[tier]
    if not spec.verifiable:
        raise UnverifiableTier(
            f"{tier.value}({spec.name})는 보증하지 않는 등급이다 — 검증 결과를"
            f" 낼 수 없다. {spec.summary}"
        )
    return spec
