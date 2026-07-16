"""대화 재구성·응답 정제 검증 — 문맥 있는 답장의 전제 (ADR-009/012).

reply 프롬프트가 대화를 흐름으로 보려면 (1) Working Memory에서 이 플레이어와의
턴을 시간순으로 뽑고, (2) 소형 모델의 사고흐름 누출을 자를 수 있어야 한다.
"""

from datetime import UTC, datetime

from lf_actor.client import sanitize_reply
from lf_actor.context import WorldContext, build
from lf_actor.conversation import conversation_turns
from lf_actor.persona import load_persona

from .conftest import PERSONAS_DIR

WORLD = WorldContext(world_id="w_t", tick=5, world_time=datetime(2026, 3, 1, tzinfo=UTC))


def wm_dm(player: str, text: str) -> str:
    return f'플레이어 {player}의 DM: "{text}"'


def wm_reply(player: str, text: str) -> str:
    return f'tick 3: 나는 플레이어 {player}에게 답했다 — "{text}"'


def test_turns_are_chronological_and_player_scoped():
    # Working Memory는 최신 우선 — 무드/타 플레이어 라인이 섞여 있다
    working = [
        wm_dm("p_a", "내가 더 상담해줄까?"),  # 최신
        "지금 기분: 담담한, 차분한 상태",
        wm_reply("p_a", "응, 근데 지금은 좀 바빠."),
        wm_dm("p_b", "안녕 민지야"),  # 다른 플레이어 — 이 대화가 아니다
        wm_dm("p_a", "참지마"),  # 가장 오래됨
    ]
    turns = conversation_turns(working, "p_a")
    assert turns == [
        ("player", "참지마"),
        ("me", "응, 근데 지금은 좀 바빠."),
        ("player", "내가 더 상담해줄까?"),
    ]


def test_comment_lines_are_dialogue_too():
    working = ['플레이어 p_a가 내 글에 댓글을 남겼다: "먼저 다가간 게 멋져요"']
    assert conversation_turns(working, "p_a") == [("player", "먼저 다가간 게 멋져요")]


def test_empty_when_no_turns_with_player():
    assert conversation_turns(["지금 기분: 밝은 상태"], "p_a") == []


def test_reply_bundle_carries_conversation_and_answer_marker():
    persona = load_persona(PERSONAS_DIR / "minji-kim.yaml")
    turns = [("player", "참지마"), ("me", "고마워"), ("player", "상담해줄까?")]
    bundle = build(
        persona, working=["지금 기분: 담담한 상태"], world=WORLD,
        purpose="reply_to_player", conversation=turns,
    )
    assert "## 이 사람과 나눈 대화 (오래된 순)" in bundle.user
    assert "- 관찰자: 참지마" in bundle.user
    assert "- 나: 고마워" in bundle.user
    # 마지막 상대 발화에만 답할 지점이 찍힌다
    assert "상담해줄까?   ← 지금 이 말에 답하라" in bundle.user
    assert bundle.user.count("← 지금 이 말에 답하라") == 1


def test_decide_bundle_has_no_conversation_section():
    persona = load_persona(PERSONAS_DIR / "minji-kim.yaml")
    bundle = build(persona, working=[], world=WORLD)  # decide_action 기본
    assert "이 사람과 나눈 대화" not in bundle.user


def test_sanitize_trims_han_chain_of_thought_after_korean():
    leaked = "팀장한테는 솔직하게 말해볼까 想着该如何回复后再作答"
    assert sanitize_reply(leaked) == "팀장한테는 솔직하게 말해볼까"


def test_sanitize_leaves_clean_korean_untouched():
    clean = "응, 근데 지금은 좀 바빠. 나중에 얘기하자."
    assert sanitize_reply(clean) == clean


def test_sanitize_does_not_destroy_pure_non_korean():
    # 한글이 없으면 판단 근거가 없다 — 건드리지 않는다
    assert sanitize_reply("OK, let's talk later.") == "OK, let's talk later."
