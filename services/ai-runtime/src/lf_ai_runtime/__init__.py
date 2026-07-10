"""lf-ai-runtime — 모든 모델 호출(LLM+임베딩)의 단일 통제 지점 (ADR-018).

엔진은 NATS request-reply로만 호출한다. SDK 직접 사용 금지.
정책: task×tier 모델 라우팅, 구조화 출력 강제, prompt caching,
세계별 예산 하드 캡, 폴백/서킷브레이커, Langfuse 트레이싱.
"""
