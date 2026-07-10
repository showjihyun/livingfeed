"""lf-actor — Actor Runtime (ADR-012).

논리 액터 = identity + state + mailbox + behavior.
인지 루프: perceive → appraise → decide → act → after-effect (docs/plan/06).
규칙: 액터 내 직렬·액터 간 병렬, 상호작용 우선, 상태 변경 = 이벤트 발행.
"""
