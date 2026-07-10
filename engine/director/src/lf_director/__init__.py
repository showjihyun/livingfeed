"""lf-director — Director AI (ADR-013).

관찰(규칙: drama score, narrative gravity, 침체 감지)은 매 tick,
개입 결정(LLM)은 임계 초과 시만. 발행 가능 이벤트는 world.* / system.director.* 뿐
(permissions.yaml에서 강제). 액터 직접 조작 금지 — 간접 개입 도구 5종만.
"""
