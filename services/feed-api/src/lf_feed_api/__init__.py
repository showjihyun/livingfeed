"""lf-feed-api — 피드 조회 API (ADR-014).

OpenSearch/Redis 프로젝션에서 읽는다. 이벤트 스토어 직접 질의 금지 (ADR-003).
예산: 피드 목록 읽기 p95 < 200ms (ADR-020).
"""
