"""lf-feed — Feed Engine 도메인 로직 (ADR-014).

2단 구조의 1단(편집): 이벤트 → feed-worthiness 점수 → FeedItem 승격.
가시성 6등급은 FeedItem 속성이다 — 파이프라인 복제 금지.
랭킹 계수는 설정값, 참여 단일 목표 최적화 금지 + 다양성 보정 필수.
"""
