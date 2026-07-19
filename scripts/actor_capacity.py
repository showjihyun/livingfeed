"""세계 규모 계획 미리보기 — 액터 수 → LLM 모델/vRAM 권장 (설정 전 확인용).

액터 수(LF_MAX_ACTORS)를 정하기 전에 필요한 모델 크기·vRAM을 표로 확인한다.
실제 적용은 액터 엔진이 부팅 시 같은 계산(capacity.py)을 로그로 찍는다.

실행:
    uv run python scripts/actor_capacity.py --actors 50
    uv run python scripts/actor_capacity.py --actors 50 --model-b 8   # 내 모델과 대조
    uv run python scripts/actor_capacity.py --table                    # 규모별 참고표
"""

from __future__ import annotations

import argparse
import sys

from lf_actor.capacity import (
    LOCAL_LLM_SOFT_CAP,
    MODEL_FLOOR_B_ABOVE_CAP,
    format_plan,
    recommend_capacity,
)

_TABLE_TIERS = [10, 15, 20, 30, 50, 100, 200, 500, 1000]


def print_table() -> None:
    print(
        f"규모별 권장 ({LOCAL_LLM_SOFT_CAP}명↑ → {MODEL_FLOOR_B_ABOVE_CAP}B 하한, "
        f"로컬 한계 ~{LOCAL_LLM_SOFT_CAP}명 미만)\n"
    )
    header = (
        f"{'액터':>5} | {'최소모델':>7} | {'권장모델':>7} | "
        f"{'vRAM(Q4)':>9} | {'vRAM(fp16)':>10} | 참고 GPU"
    )
    print(header)
    print("-" * len(header))
    for n in _TABLE_TIERS:
        p = recommend_capacity(n)
        floor = f"{p.recommended_min_params_b}B"
        comfort = f"~{p.comfort_params_b}B"
        print(
            f"{p.actor_count:>5} | {floor:>7} | {comfort:>7} | "
            f"{p.vram_q4_gb:>7}GB | {p.vram_fp16_gb:>8}GB | {p.gpu_hint}"
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="세계 규모(액터 수) → LLM/vRAM 권장")
    parser.add_argument("--actors", type=int, help="액터 수 (10~1000, 범위 밖은 클램프)")
    parser.add_argument("--model-b", type=float, default=None,
                        help="내 LLM 모델 파라미터(B) — 하한 위반 경고 대조용 (선택)")
    parser.add_argument("--table", action="store_true", help="규모별 참고표 출력")
    args = parser.parse_args()

    if args.table or args.actors is None:
        print_table()
        if args.actors is None:
            return
        print()

    if args.actors is not None:
        print(format_plan(recommend_capacity(args.actors, args.model_b)))


if __name__ == "__main__":
    main()
