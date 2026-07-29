"""작업 기억에 남는 자기 기록 — 한 곳에서만 만든다 (ADR-008, ADR-021 §4 L1).

엔진(RESOLVE)이 쓰고 L1 러너가 되짚는 문장들이다. 두 곳에서 따로 만들면
포맷이 갈리는 순간 러너가 "어긋났다"고 보고하는데, 그건 세계의 사고가 아니라
우리 코드의 사고다 — 없는 회귀를 만드는 셈이다. 그래서 원천을 하나로 둔다.

전부 **적재된 봉투의 순수 함수**다: 상태를 보지 않으므로, 로그만 있으면 그때
그 인물이 자기에 대해 무엇을 적었는지 그대로 다시 만들 수 있다. 이름 그라운딩에
쓰는 명부(roster)만 밖에서 온다 — 이름은 페르소나의 것이라 이벤트가 아니다.
"""

from __future__ import annotations

from typing import Any

from lf_actor.consolidation import action_label


def action_memo(tick: int, payload: dict[str, Any]) -> str:
    """자기 행동 — decide가 확정한 의도 그대로."""
    return f"tick {tick}: 나는 {action_label(payload['action_kind'])} — {payload['intent']}"


def reply_to_actor_memo(tick: int, who: str, text: str) -> str:
    """액터 댓글에의 답글 (액터 소셜 루프)."""
    return f'tick {tick}: 나는 {who}의 댓글에 답했다 — "{text}"'


def reply_to_player_memo(tick: int, player_id: str, text: str) -> str:
    """플레이어 개입에의 응답 (상호작용 우선, ADR-012 규칙 2)."""
    return f'tick {tick}: 나는 플레이어 {player_id}에게 답했다 — "{text}"'


def proactive_dm_memo(tick: int, player_id: str, text: str) -> str:
    """'기억됨' 선제 DM (plan/02)."""
    return f'tick {tick}: 나는 플레이어 {player_id}에게 먼저 안부를 건넸다 — "{text}"'


def spontaneous_comment_memo(tick: int, name: str, text: str) -> str:
    """이웃의 글에 남긴 자발 댓글."""
    return f'tick {tick}: 나는 {name}의 글에 댓글을 남겼다 — "{text}"'


def belief_memo(statement: str) -> str:
    """곱씹어 굳은 생각 — tick을 달지 않는다 (기간의 결론이라 순간이 아니다)."""
    return f"곱씹은 생각: {statement}"


def memo_for_own_event(
    envelope: dict[str, Any], roster: dict[str, str]
) -> str | None:
    """액터 자신이 낸 봉투 하나 → 작업 기억 줄. 남길 것이 없으면 None.

    RESOLVE가 만드는 것과 같은 문장을 봉투만 보고 되짚는다 — L1 러너의 진입점이다.
    message.sent의 세 갈래(플레이어 응답 / 액터 댓글 답글 / 선제 DM / 자발 댓글)는
    payload가 스스로 구분한다: 상대가 플레이어인지 액터인지, 답글인지 최상위인지.
    """
    kind = envelope["type"]
    tick = envelope["tick"]
    payload = envelope["payload"]

    if kind == "actor.action.performed":
        return action_memo(tick, payload)
    if kind == "actor.belief.formed":
        return belief_memo(payload["statement"])
    if kind != "actor.message.sent":
        return None

    text = payload["text"]
    player_id = payload.get("target_player_id")
    if player_id:
        # in_reply_to가 없으면 먼저 건넨 안부다 (선제 DM은 사슬의 시작 — phases 주석)
        if payload.get("in_reply_to") is None:
            return proactive_dm_memo(tick, player_id, text)
        return reply_to_player_memo(tick, player_id, text)

    target = payload.get("target_actor_id")
    if not target:
        return None
    name = roster.get(target, target)
    # 최상위 댓글은 in_reply_to == post_id다 (social.comment_targets의 판정과 동형).
    # 답글은 그 댓글의 event_id를 가리켜 둘이 갈린다.
    if payload.get("post_id") and payload.get("in_reply_to") == payload.get("post_id"):
        return spontaneous_comment_memo(tick, name, text)
    return reply_to_actor_memo(tick, name, text)
