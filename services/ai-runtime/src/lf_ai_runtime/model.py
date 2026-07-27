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
class Completion:
    """프로바이더 한 호출의 결과 — 스키마 검증 전 구조화 출력 후보 + 계량기.

    usage를 응답과 한 몸으로 돌린다: 프로바이더 인스턴스에 마지막 usage를
    얹어두는 방식은 동시 호출(LF_AI_CONCURRENCY)에서 서로의 값을 덮는다.
    """

    output: dict[str, Any]
    usage: Usage = Usage()


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

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "model": self.model, "error": self.error}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> InferenceResponse:
        return cls(
            ok=bool(data.get("ok")),
            output=data.get("output"),
            model=data.get("model"),
            error=data.get("error"),
        )


def infer_subject(env: str) -> str:
    """AI Runtime request-reply subject (ADR-018 — NATS 전송로)."""
    return f"lf.{env}.ai.infer"
