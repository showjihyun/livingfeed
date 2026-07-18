"""화면 문장 정화 — 원시 id를 read 경계에서 세계 어휘로 (공용).

es는 불변이다 — composer의 쓰기 시점 정화(engine/feed compose.humanize_ids)
이전에 발행된 포스트 본문에는 `p_observer_…에게` 같은 원시 id가 그대로
남아 OS 인덱스·es에 실려 온다. 원본은 사실, 화면은 문장: 읽기 경계에서
같은 규칙을 한 번 더 건다 (projector 리시트 정화의 확장).

compose.humanize_ids와 규칙이 같다 — 패키지 경계상 공유가 불가해 규칙을
복제한다 (변경 시 양쪽 동기화).
"""

from __future__ import annotations

import re

# \b는 못 쓴다 — 유니코드 \w에 한글이 포함돼 'p_x에게' 같은 조사 접합의
# 끝 경계가 성립하지 않는다. ASCII 영숫자가 앞에 붙은 경우만 배제한다
# (grasp_… 같은 영단어 오염 방지 — compose.humanize_ids와 같은 규칙).
_ACTOR_REF = re.compile(r"(?<![A-Za-z0-9])a_[a-z0-9_]+")
_PLAYER_REF = re.compile(r"(?<![A-Za-z0-9])p_[a-z0-9_]+")

#: 관찰자 익명성 — 플레이어 id의 세계 안 어휘 (compose·story 규약과 동일)
PLAYER_ALIAS = "어느 관찰자"


def humanize_ids(text: str, actor_names: dict[str, str]) -> str:
    """액터 id는 실명 그라운딩(모르면 '누군가'), 플레이어 id는 '어느 관찰자'."""
    text = _ACTOR_REF.sub(lambda m: actor_names.get(m.group(0), "누군가"), text)
    return _PLAYER_REF.sub(PLAYER_ALIAS, text)


def anonymize_players(text: str) -> str:
    """플레이어 id만 '어느 관찰자'로 — 액터 id는 그대로 둔다.

    이름 원천(read.actors·personas)에 닿지 않는 계층용: 액터 id를 '누군가'로
    뭉개면 그라운딩 기회만 잃는다 — 이름 해석은 FE(authorName 로스터)·story
    경로의 몫이다. 관찰자 익명성(플레이어 id)만 이 계층의 책임이다.
    """
    return _PLAYER_REF.sub(PLAYER_ALIAS, text)


def find_actor_refs(text: str) -> set[str]:
    """문장 속 액터 id 참조 수집 — 이름 그라운딩(read.actors 조회)의 사전 단계."""
    return set(_ACTOR_REF.findall(text))
