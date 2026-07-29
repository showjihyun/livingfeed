"""AI Runtime 인터페이스 — 모델이 아니라 '작업'을 요청한다 (ADR-018).

호출자는 모델명을 모른다. task × tier → 모델 매핑은 AI Runtime의 설정이다.
전송은 NATS request-reply(JSON)이며, 이 모듈이 그 wire 형식을 소유한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TASKS = frozenset(
    {"decide_action", "converse", "narrate", "summarize", "reflect", "director_plan", "embed"}
)
TIERS = frozenset({"hot", "warm", "system"})


@dataclass(frozen=True)
class Usage:
    """한 호출이 실제로 쓴 토큰 — 비용 집행의 계량기 (ADR-018 §3).

    input_tokens는 **캐시 밖 입력만** 담는다: 프로바이더 어댑터가 캐시 적중분을
    cache_read_tokens로 떼어 옮기므로 단가 계산에서 겹세지 않는다 (pricing.py).
    프로바이더가 usage를 주지 않으면(로컬 서버 등) 전부 0 — 비용 0으로 셈해진다.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(frozen=True)
class Sampling:
    """이번 호출이 **실제로 보낸** 샘플링 파라미터 (ADR-021 §2).

    None은 "우리가 지정하지 않았다 = 프로바이더 기본값"이라는 뜻이다. '모른다'가
    아니라 '보내지 않았다'로 읽어야 한다 — 재현이 안 될 때 무엇이 달랐는지
    좁히려면 그 둘의 구분이 결정적이다.

    지정하지 않는 것이 기본인 이유: 온도를 우리가 박아 넣으면 프로바이더가
    모델별로 고른 기본값을 덮어써 세계의 결이 조용히 달라진다. 실험이 필요할
    때만 LF_AI_TEMPERATURE 등으로 명시해 고정한다 (그 고정 자체가 기록에 남는다).
    """

    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    max_output_tokens: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
            "max_output_tokens": self.max_output_tokens,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Sampling | None:
        if data is None:
            return None
        return cls(
            temperature=data.get("temperature"),
            top_p=data.get("top_p"),
            seed=data.get("seed"),
            max_output_tokens=data.get("max_output_tokens"),
        )

    def sent_kwargs(self) -> dict[str, Any]:
        """프로바이더 호출에 실을 인자 — 지정된 것만 (None은 아예 보내지 않는다)."""
        return {
            name: value
            for name, value in (
                ("temperature", self.temperature),
                ("top_p", self.top_p),
                ("seed", self.seed),
            )
            if value is not None
        }


@dataclass(frozen=True)
class Completion:
    """프로바이더 한 호출의 결과 — 스키마 검증 전 구조화 출력 후보 + 계량기.

    usage를 응답과 한 몸으로 돌린다: 프로바이더 인스턴스에 마지막 usage를
    얹어두는 방식은 동시 호출(LF_AI_CONCURRENCY)에서 서로의 값을 덮는다.
    """

    output: dict[str, Any]
    usage: Usage = Usage()
    #: 이 호출이 실제로 보낸 샘플링 — 규칙 프로바이더는 부른 모델이 없어 None
    sampling: Sampling | None = None


@dataclass(frozen=True)
class ContextBundle:
    """Context Fabric이 조립한 프롬프트 (ADR-009).

    system은 변동성 낮은 프리픽스(정체성 등 — prompt cache 대상, ADR-018 §2),
    user는 호출별 변동 섹션이다. 섹션 순서 책임은 Context Fabric에 있다.
    """

    system: str
    user: str
    trace_id: str


@dataclass(frozen=True)
class InferenceRequest:
    task: str
    bundle: ContextBundle
    output_schema: dict[str, Any]
    actor_tier: str  # hot | warm | system (ADR-011 LOD)
    trace: dict[str, Any] = field(default_factory=dict)  # trace_id, actor_id, tick

    def to_json(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "bundle": {
                "system": self.bundle.system,
                "user": self.bundle.user,
                "trace_id": self.bundle.trace_id,
            },
            "output_schema": self.output_schema,
            "actor_tier": self.actor_tier,
            "trace": self.trace,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> InferenceRequest:
        if data.get("task") not in TASKS:
            raise ValueError(f"알 수 없는 task: {data.get('task')}")
        if data.get("actor_tier") not in TIERS:
            raise ValueError(f"알 수 없는 tier: {data.get('actor_tier')}")
        bundle = data["bundle"]
        return cls(
            task=data["task"],
            bundle=ContextBundle(
                system=bundle["system"], user=bundle["user"], trace_id=bundle["trace_id"]
            ),
            output_schema=data["output_schema"],
            actor_tier=data["actor_tier"],
            trace=data.get("trace", {}),
        )


@dataclass(frozen=True)
class InferenceResponse:
    ok: bool
    output: dict[str, Any] | None = None
    model: str | None = None
    error: str | None = None
    #: 결정 기록(actor.decision.made.sampling)의 원천 — 엔진이 그대로 싣는다
    sampling: Sampling | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "model": self.model,
            "error": self.error,
            "sampling": None if self.sampling is None else self.sampling.to_json(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> InferenceResponse:
        return cls(
            ok=bool(data.get("ok")),
            output=data.get("output"),
            model=data.get("model"),
            error=data.get("error"),
            sampling=Sampling.from_json(data.get("sampling")),
        )


def infer_subject(env: str) -> str:
    """AI Runtime request-reply subject (ADR-018 — NATS 전송로)."""
    return f"lf.{env}.ai.infer"
