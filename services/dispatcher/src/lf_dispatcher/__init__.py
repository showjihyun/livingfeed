"""lf-dispatcher — Event Dispatcher (ADR-017).

책임: outbox relay(발행의 유일한 경로), 스키마 게이트, 발행 권한 매트릭스,
subject 라우팅, DLQ 관리. 단일 활성 인스턴스 (PG advisory lock leader election).
"""
