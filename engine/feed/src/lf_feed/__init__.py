"""lf-feed — Feed Engine 도메인 로직 (ADR-014).

2단 구조의 1단(편집): 이벤트 → feed-worthiness 점수 → FeedItem 승격.
가시성 6등급은 FeedItem 속성이다 — 파이프라인 복제 금지.
랭킹 계수는 설정값, 참여 단일 목표 최적화 금지 + 다양성 보정 필수.

실행 엔트리: python -m lf_feed.main (JetStream LF_ACTOR 소비 → append 적재).
"""

from lf_feed.composer import FeedComposer as FeedComposer
from lf_feed.config import Config as Config
from lf_feed.scoring import ScoringConfig as ScoringConfig
