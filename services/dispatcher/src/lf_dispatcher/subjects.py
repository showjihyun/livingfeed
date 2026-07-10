"""NATS subject 생성 — 유일한 구현 (ADR-017 §3).

서비스 코드에 subject 문자열 하드코딩 금지. 반드시 이 함수를 사용한다.
문법 변경은 ADR-017 개정 사항이다.
"""

from __future__ import annotations

STREAMS = frozenset({"actor", "world", "relationship", "feed", "player", "system"})


def subject(env: str, world_id: str, stream: str, event_type: str) -> str:
    """lf.<env>.<world_id>.<stream>.<type> (ADR-004 §Subject 체계).

    event_type은 '<stream>.' 접두를 포함한 전체 타입명이다.
    예: subject("dev", "w_main", "actor", "actor.action.performed")
        → "lf.dev.w_main.actor.action.performed"
    """
    if stream not in STREAMS:
        raise ValueError(f"알 수 없는 stream: {stream}")
    if not event_type.startswith(f"{stream}."):
        raise ValueError(f"이벤트 타입 '{event_type}'이 stream '{stream}'에 속하지 않는다")
    return f"lf.{env}.{world_id}.{event_type}"


def dlq_subject(env: str, original_subject: str) -> str:
    """DLQ 이동 대상 subject (ADR-017 §4)."""
    return f"lf.{env}.dlq.{original_subject.removeprefix(f'lf.{env}.')}"
