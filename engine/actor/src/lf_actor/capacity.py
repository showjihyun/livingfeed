"""세계 규모 계획 — 액터 수 조정 + LLM 모델/vRAM 권장 (순수 자문).

설정(LF_MAX_ACTORS)으로 세계의 액터 수를 10~1000명에서 조절한다. 액터 수가
늘수록 매 tick 동시 반응(LLM 호출)이 늘어 더 큰 모델과 vRAM이 필요하다.

핵심 규칙 (운영 경험 기반):
- 로컬 LLM 실측 한계는 ~20명 미만이다 (그 이상은 반응 지연·품질 저하).
- **20명 이상이면 40B 이상 모델만 권장한다** (작은 모델은 규모에서 무너진다).
- 400명 이상은 로컬 vRAM보다 호스티드 API를 권장한다.

vRAM 수치는 (모델 가중치 + 동시성 KV 헤드룸)의 근사 가이드다 — 정확한 값은
양자화·컨텍스트 길이·추론 엔진에 따라 달라진다. 보수적으로 GPU 등급으로 올림한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: 세계 규모 하한·상한 (설정 클램프 범위)
MIN_ACTORS = 10
MAX_ACTORS = 1000
#: 이 수 미만이면 작은 로컬 모델로도 실측 가능 (그 이상은 40B 하한)
LOCAL_LLM_SOFT_CAP = 20
#: 20명 이상에서 요구되는 모델 파라미터 하한 (B)
MODEL_FLOOR_B_ABOVE_CAP = 40
#: 이 규모 이상은 로컬보다 호스티드 API 권장
HOSTED_RECOMMENDED_AT = 400
#: 흔한 GPU vRAM 등급 (올림 대상, GB)
_GPU_TIERS_GB = (8, 12, 16, 24, 32, 48, 80, 120, 160, 320)


@dataclass(frozen=True)
class CapacityPlan:
    requested: int
    actor_count: int  # 클램프된 유효 수
    recommended_min_params_b: int  # 하한 (20명↑ → 40B)
    comfort_params_b: int  # 규모에 편안한 권장 모델
    vram_q4_gb: int  # 4비트 양자화 기준 권장 vRAM
    vram_fp16_gb: int  # fp16(비양자화) 기준 권장 vRAM
    gpu_hint: str
    hosted_recommended: bool
    warnings: list[str] = field(default_factory=list)


def clamp_actor_count(n: int) -> int:
    """액터 수를 [MIN_ACTORS, MAX_ACTORS]로 클램프."""
    return max(MIN_ACTORS, min(MAX_ACTORS, int(n)))


def _comfort_params_b(n: int) -> int:
    """규모에 편안한 권장 모델 크기 (하한과 별개 — 반응 품질 기준)."""
    if n < LOCAL_LLM_SOFT_CAP:
        return 7
    if n < 50:
        return 40
    if n < 150:
        return 70
    return 120


def _recommended_min_params_b(n: int) -> int:
    """모델 파라미터 하한 — 20명 이상은 40B (사용자 규칙)."""
    return 7 if n < LOCAL_LLM_SOFT_CAP else MODEL_FLOOR_B_ABOVE_CAP


def _concurrency(n: int) -> int:
    """동시 LLM 호출 근사 — 개입 반응 최우선 예산 + LOD(대부분 Warm/Cold) + 세마포어(~16)."""
    return min(n, 16)


def _vram_gb(params_b: float, concurrency: int, bytes_per_param: float) -> float:
    weights = params_b * bytes_per_param
    # KV/컨텍스트 헤드룸 — 동시 시퀀스 수·모델 크기에 대략 비례
    kv_headroom = concurrency * 0.35 * (params_b / 40.0)
    return weights + kv_headroom


def _round_to_gpu(gb: float) -> int:
    for tier in _GPU_TIERS_GB:
        if gb <= tier:
            return tier
    return int(math.ceil(gb / 80.0) * 80)


def _gpu_hint(vram_q4_gb: int, hosted: bool) -> str:
    if hosted:
        return "다중 GPU 또는 호스티드 API (Claude 등) 권장"
    table = {
        8: "8GB급 (RTX 3050/3060)",
        12: "12GB급 (RTX 3060 12G / 4070)",
        16: "16GB급 (RTX 4080 / 4060 Ti 16G)",
        24: "24GB급 (RTX 3090 / 4090)",
        32: "32GB급 (V100 32G / 다중 24G)",
        48: "48GB급 (RTX A6000 / L40S)",
        80: "80GB급 (A100 80G / H100)",
        120: "다중 80GB GPU",
        160: "다중 80GB GPU",
        320: "다중 GPU 클러스터",
    }
    return table.get(vram_q4_gb, "다중 GPU 또는 호스티드 API 권장")


def recommend_capacity(
    requested_count: int, model_params_b: float | None = None
) -> CapacityPlan:
    """요청 액터 수 → 규모 계획 (클램프 + 모델/vRAM 권장 + 경고).

    model_params_b: 설정된 LLM 모델 파라미터(B). 알면 하한 위반을 구체 경고한다.
    """
    n = clamp_actor_count(requested_count)
    comfort = _comfort_params_b(n)
    floor = _recommended_min_params_b(n)
    conc = _concurrency(n)
    vram_q4 = _round_to_gpu(_vram_gb(comfort, conc, 0.6))  # ~4비트 양자화
    vram_fp16 = _round_to_gpu(_vram_gb(comfort, conc, 2.0))  # fp16
    hosted = n >= HOSTED_RECOMMENDED_AT

    warnings: list[str] = []
    if int(requested_count) != n:
        warnings.append(
            f"액터 수는 {MIN_ACTORS}~{MAX_ACTORS} 범위로 클램프됩니다 "
            f"({int(requested_count)} → {n})."
        )
    if n >= LOCAL_LLM_SOFT_CAP:
        warnings.append(
            f"{n}명은 로컬 LLM 실측 한계(~{LOCAL_LLM_SOFT_CAP}명 미만)를 넘습니다 — "
            f"{MODEL_FLOOR_B_ABOVE_CAP}B 이상 모델만 권장합니다."
        )
        if model_params_b is not None and model_params_b < MODEL_FLOOR_B_ABOVE_CAP:
            warnings.append(
                f"⚠️ 설정된 모델이 {model_params_b:g}B로 하한({MODEL_FLOOR_B_ABOVE_CAP}B) "
                f"미만입니다 — {n}명 규모에서는 반응 품질·지연이 무너집니다. "
                f"모델을 키우거나 액터 수를 {LOCAL_LLM_SOFT_CAP - 1}명 이하로 낮추세요."
            )
    if hosted:
        warnings.append(
            f"{n}명 규모는 로컬 vRAM 대신 호스티드 API 사용을 권장합니다 "
            f"(비용·동시성 관리는 ADR-018 예산 라우팅)."
        )

    return CapacityPlan(
        requested=int(requested_count),
        actor_count=n,
        recommended_min_params_b=floor,
        comfort_params_b=comfort,
        vram_q4_gb=vram_q4,
        vram_fp16_gb=vram_fp16,
        gpu_hint=_gpu_hint(vram_q4, hosted),
        hosted_recommended=hosted,
        warnings=warnings,
    )


def format_plan(plan: CapacityPlan) -> str:
    """계획을 사람이 읽는 여러 줄 문자열로 (로그·CLI 공용)."""
    floor_note = (
        f"{plan.recommended_min_params_b}B 이상 (20명↑ 하한)"
        if plan.actor_count >= LOCAL_LLM_SOFT_CAP
        else f"{plan.recommended_min_params_b}B급 (소형 로컬 가능)"
    )
    lines = [
        f"세계 규모 계획 — 액터 {plan.actor_count}명",
        f"  권장 최소 모델: {floor_note}",
        f"  편안한 권장 모델: ~{plan.comfort_params_b}B",
        f"  권장 vRAM: {plan.vram_q4_gb}GB (4비트 양자화) / {plan.vram_fp16_gb}GB (fp16)",
        f"  참고 GPU: {plan.gpu_hint}",
    ]
    lines.extend(f"  • {w}" for w in plan.warnings)
    return "\n".join(lines)
