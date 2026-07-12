"""lf-projector — Projection Workers (ADR-003).

프로젝터 계약: 멱등 / durable consumer 체크포인트 / --rebuild 재구축 가능 /
이벤트 발행 금지 / 프로젝터 간 격리.
종류: pg(읽기 테이블), kuzu(관계 그래프+graph query API), qdrant(의미 기억),
os(피드 검색), redis(hot state).

구현 현황: os(피드 검색, 로드맵 7단계) — 나머지는 각자의 로드맵 단계에서 추가.
실행: python -m lf_projector.main --kind os [--rebuild]
"""
