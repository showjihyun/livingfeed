"""lf-tick — Tick Engine (ADR-011).

이산 tick 파이프라인: WORLD → PERCEIVE → DECIDE → RESOLVE → CONSOLIDATE.
tick이 늦으면 미루고 완주한다 — 건너뛰기 금지. 세계당 1 인스턴스 (leader election).
"""

from lf_tick.clock import TickClock as TickClock
