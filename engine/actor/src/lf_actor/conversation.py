"""대화 재구성 — Working Memory의 대화 라인을 플레이어별 시간순 턴으로 (ADR-009/012).

reply_to_player 프롬프트가 대화를 '흐름'으로 보게 한다: 최신순으로 뒤섞인
버퍼가 아니라 오래된→최근 순서의 주고받음. 직접 질문에 답하고 말투를
유지하는 문맥이 여기서 생긴다.

프로젝션(read.messages)을 읽지 않는다 — 액터는 자기 Working Memory만 본다.
대화 라인의 형식은 consolidation.describe_interaction과 phases의 응답 메모가
정하며, 여기 정규식은 그 형식의 역파서다 (형식이 바뀌면 함께 바뀐다).
"""

from __future__ import annotations

import re

#: describe_interaction의 DM/댓글 지각 라인
_DM_IN = re.compile(r'^플레이어 (?P<pid>\S+)의 DM: "(?P<text>.*)"$', re.S)
_COMMENT_IN = re.compile(r'^플레이어 (?P<pid>\S+)가 내 글에 댓글을 남겼다: "(?P<text>.*)"$', re.S)
#: phases.resolve가 남기는 자기 응답 메모 (앞에 "tick N: "가 붙는다)
_REPLY_OUT = re.compile(r'나는 플레이어 (?P<pid>\S+)에게 답했다 — "(?P<text>.*)"$', re.S)

#: speaker 상수 — 화면 라벨이 아니라 역할 (프롬프트 조립이 라벨을 붙인다)
PLAYER = "player"
SELF = "me"


def conversation_turns(
    working_newest_first: list[str], player_id: str
) -> list[tuple[str, str]]:
    """(speaker, text) 시간순(오래된→최근). speaker ∈ {PLAYER, SELF}.

    Working Memory는 최신 우선 저장이라 뒤집어 읽는다. 이 플레이어와 무관한
    라인(다른 플레이어, 무드 요약, 행동 메모)은 대화가 아니므로 건너뛴다.
    """
    turns: list[tuple[str, str]] = []
    for line in reversed(working_newest_first):
        for pattern, speaker, anchored in (
            (_DM_IN, PLAYER, True),
            (_COMMENT_IN, PLAYER, True),
            (_REPLY_OUT, SELF, False),
        ):
            match = pattern.match(line) if anchored else pattern.search(line)
            if match and match.group("pid") == player_id:
                turns.append((speaker, match.group("text")))
                break
    return turns
