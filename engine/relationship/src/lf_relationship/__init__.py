"""lf-relationship — Relationship Engine (ADR-016).

A→B 방향별 독립 5차원(trust/intimacy/respect/attraction/resentment) + stage.
stage 전이는 수치가 아니라 이벤트(행동)로만. 관계는 sparse — 상호작용 시 생성,
액터당 활성 상한 150 (Dunbar). 갱신은 규칙 기반 (ADR-015와 동일 논거).
"""
