"""lf-gateway — API Gateway + Transport Abstraction Layer (ADR-010).

SSE(피드 스트림)·WebSocket(상호작용 세션)·REST를 담당한다.
무상태 — 구독 필터·세션 상태는 Redis에 둔다 (ADR-019 HPA 대상).
"""
