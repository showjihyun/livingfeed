"""읽기 경계 정화 규칙 — 원시 id는 화면 문장에 실리지 않는다 (sanitize.py).

compose.humanize_ids(쓰기 정화)와 같은 규칙의 읽기측 복제 — 함정 선례까지
같이 고정한다: \\b는 유니코드 \\w에 한글이 포함돼 'p_x에게'의 끝 경계가
성립하지 않는다 (ASCII lookbehind 규칙의 회귀 방지).
"""

from lf_feed_api.sanitize import anonymize_players, find_actor_refs, humanize_ids

NAMES = {"a_aria_kim": "김아리"}


def test_actor_ids_ground_to_names_and_unknown_is_someone():
    assert humanize_ids("a_aria_kim가 웃었다", NAMES) == "김아리가 웃었다"
    assert humanize_ids("a_ghost의 시선", NAMES) == "누군가의 시선"


def test_player_ids_become_observer():
    assert humanize_ids("p_observer_0417에게 고맙다", NAMES) == "어느 관찰자에게 고맙다"
    assert anonymize_players("p_observer_0417에게 고맙다") == "어느 관찰자에게 고맙다"


def test_hangul_josa_boundary_is_matched():
    # 함정 선례: \b였다면 'p_…0417에게'의 끝 경계가 성립하지 않아 잔존했다
    out = humanize_ids("…p_observer_0417에게 전하고 싶은 말", NAMES)
    assert "p_observer" not in out and "어느 관찰자에게" in out


def test_ascii_embedded_tokens_are_not_ids():
    # grasp_… 같은 영단어 속 a_/p_ 조각은 id가 아니다 (ASCII lookbehind)
    text = "grasp_thing과 vespa_kim은 그대로"
    assert humanize_ids(text, NAMES) == text


def test_anonymize_players_leaves_actor_ids_alone():
    # 이름 원천이 없는 계층(FeedSearch 미주입) — 액터 id는 '누군가'로 뭉개지 않는다
    assert anonymize_players("a_aria_kim와 p_observer_0417") == "a_aria_kim와 어느 관찰자"


def test_find_actor_refs_collects_grounding_targets():
    assert find_actor_refs("a_aria_kim와 a_junho_park, 그리고 p_observer_0417") == {
        "a_aria_kim", "a_junho_park",
    }
    assert find_actor_refs("아무 id 없는 문장") == set()
