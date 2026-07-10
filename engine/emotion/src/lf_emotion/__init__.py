"""lf-emotion — Emotion Engine (ADR-015).

2층 구조: mood(PAD 3차원, 느림) + emotion 인스턴스(대상·출처 있음, 빠름).
전 과정 규칙 기반 — LLM은 감정 계산에 관여하지 않는다 (리플레이 재현성).
파라미터는 params.yaml 단일 파일로 관리한다.
"""
