"""Context Fabric 최소 구현 (ADR-009).

섹션 순서는 고정이다 — 변동성 낮은 것부터. 이 순서가 prompt cache 적중률을
만든다 (ADR-018 §2). 순서 변경은 ADR-009 대체 없이는 금지.

Phase 1 범위: Identity(1) / Relationship(3) / Episodes(4) / Working(5) / World(6) /
Task Frame(7). Beliefs(2)는 해당 계층 도입 시 채워진다.
순수 함수적: 같은 입력 → 같은 번들 (trace_id 제외). LLM 호출·상태 변경 없음.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from lf_eventstore import new_ulid

from lf_actor.arc import Arc
from lf_actor.persona import Persona
from lf_actor.semantic import Recollection

#: 조립기 버전 — 섹션 구성·순서·문구를 바꾸면 **반드시 올린다** (ADR-021 §2).
#: 이 값이 bundle_digest의 접두라, 올리지 않고 조립을 바꾸면 과거 결정의 재조립이
#: '검증 불가'가 아니라 '검증 실패'로 보고된다 — 없는 사고를 만드는 쪽이 더 나쁘다.
ASSEMBLER_VERSION = "v1"

#: 문자 기반 토큰 근사 — 예산 집행용 보수적 환산 (한글 혼용 기준 1 token ≈ 2.5 chars)
CHARS_PER_TOKEN = 2.5

#: 섹션별 토큰 예산 (ADR-009 §토큰 예산, Phase 1 기본값)
BUDGET_IDENTITY = 800
BUDGET_RELATIONSHIPS = 300
BUDGET_EPISODES = 600
BUDGET_WORKING = 1_200
BUDGET_WORLD = 400
BUDGET_TASK_FRAME = 600
BUDGET_SEEN_POSTS = 400

#: 인생 단계 → 한글 라벨 (docs/plan/08 Life Journey). 아크 섹션 표기용
STAGE_LABELS = {
    "student": "학생기",
    "newcomer": "사회 초년기",
    "settling": "정착·방황기",
    "prime": "전성기·침체기",
    "elder": "원로기",
}


def _arc_section(arc: Arc | None) -> str:
    """인생 아크 — Director가 정한 이번 시즌 방향 (ADR-013/plan/08). 결정의 최상위 프레임.

    아크가 없으면 섹션을 생략한다 — 아직 아크를 받지 않은 액터는 그저 일상을 산다.
    """
    if arc is None:
        return ""
    label = STAGE_LABELS.get(arc.stage, arc.stage)
    return (
        "## 인생 아크 (이번 시즌 당신 인생의 방향 — 명령이 아니라 배경이다)\n"
        f"- 지금 당신은 {label}에 있다.\n"
        f"- 이 시즌 당신의 인생은 이렇게 향한다: {arc.intention}"
    )


def _relationships_section(relationships: str | None) -> str:
    """Relationship(3) — 얽힌 사람들의 온도 (ADR-016 → ADR-009 §3).

    원한이 쌓인 상대에게 태연히 말을 걸지 않으려면, 결정 앞에 관계의
    온도가 놓여야 한다. 관계가 없으면 섹션을 생략한다 — 아직 아무와도
    얽히지 않은 액터에게 빈 관계는 소음이다 (아크와 같은 규약).
    """
    if not relationships:
        return ""
    return _clip("## 얽힌 사람들 (관계의 온도)\n" + relationships, BUDGET_RELATIONSHIPS)


@dataclass(frozen=True)
class WorldContext:
    world_id: str
    tick: int
    world_time: datetime


@dataclass(frozen=True)
class Section:
    """번들의 한 섹션 — 문자열로 접히기 **전**의 모습 (ADR-021 §2).

    kind는 ADR-009의 고정 섹션 순서에서 온 이름이고, source_ids는 이 섹션에
    실제로 들어간 사건들이다. 조립 결과(system/user 문자열)만 남기면 "어떤
    기억이 이 결정에 들어갔나"를 사후에 뽑을 방법이 없다 — 결정 기록이
    참조하는 것이 이 구조다.

    source_ids가 비는 섹션이 있다: 작업 기억·대화는 Redis의 평문 줄이라 사건
    id가 없다. 그때는 비운다 — 없는 근거를 지어내지 않는 것이 계약이다.
    """

    kind: str
    text: str
    source_ids: tuple[str, ...] = ()

    @property
    def token_count(self) -> int:
        """예산 집행과 같은 자를 쓴다 (CHARS_PER_TOKEN) — 기록이 다른 자를 쓰면 대조가 안 된다."""
        return round(len(self.text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Bundle:
    """AI Runtime으로 넘어가는 조립 결과 (ADR-009 ContextBundle).

    system = 정적 프리픽스(Identity), user = 변동 섹션들.
    sections는 그 둘로 접히기 전의 원본이다 — system/user가 sections에서
    파생되므로 둘이 갈릴 수 없다 (build가 유일한 생성 지점).
    """

    system: str
    user: str
    trace_id: str
    #: 무엇을 결정하려 조립했는가 (Task Frame 이름) — 결정 기록의 실험 단위다
    purpose: str = "decide_action"
    sections: tuple[Section, ...] = ()

    @property
    def digest(self) -> str:
        """번들의 결정적 지문 — L1 재조립 검증의 근거다 (ADR-021 §2/§4).

        조립이 순수 함수(같은 입력 → 같은 번들)라는 성질을 이용한다: 리플레이
        때 같은 입력으로 다시 조립해 이 값이 일치하면 "그때 이 인물이 무엇을
        알고 있었는지"가 증명된다. **LLM 출력이 재현 불가능해도 입력은 재현
        가능하다** — 이것이 연구용 관측성의 핵심 자산이다.

        조립기 버전을 접두로 넣는다: 조립 로직이 바뀌면 과거 digest는 전부
        불일치하는데, 그것을 '검증 실패'로 읽으면 없는 사고를 보고하게 된다.
        버전이 다르면 대조하지 않고 '검증 불가'로 가른다 (verify_digest).

        trace_id는 넣지 않는다 — 호출마다 다르니 넣으면 지문이 매번 달라져
        재조립 대조라는 목적 자체가 사라진다.
        """
        payload = json.dumps(
            [[s.kind, s.text, list(s.source_ids)] for s in self.sections],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"{ASSEMBLER_VERSION}:sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


class DigestVerdict(StrEnum):
    """재조립 대조의 결과 — 세 값이며, 뒤 둘은 결코 같은 것이 아니다 (ADR-021 §2).

    MATCH        결정 시점의 컨텍스트가 그대로 재현됐다 (L1 보증 성립).
    MISMATCH     같은 조립기인데 결과가 다르다 — 실제 사고다. 입력이 달라졌거나
                 조립이 순수하지 않다는 뜻이라 조사 대상이다.
    UNVERIFIABLE 조립기 버전이 달라 대조 자체가 무의미하다. 실패가 아니라
                 '알 수 없음'이며, 이것을 실패로 뭉개면 리포트가 거짓말을 한다.
    """

    MATCH = "match"
    MISMATCH = "mismatch"
    UNVERIFIABLE = "unverifiable"


def verify_digest(recorded: str, rebuilt: str) -> DigestVerdict:
    """기록된 digest와 재조립 digest를 대조한다 (ADR-021 §4 L1).

    버전이 다르면 해시를 비교하지 않는다 — 다른 조립기의 산출물은 달라서
    정상이고, 그 불일치를 보고하면 조립기를 고칠 때마다 과거 전체가
    '어긋남'으로 보인다.
    """
    if _digest_version(recorded) != _digest_version(rebuilt):
        return DigestVerdict.UNVERIFIABLE
    return DigestVerdict.MATCH if recorded == rebuilt else DigestVerdict.MISMATCH


def _digest_version(digest: str) -> str:
    return digest.split(":", 1)[0] if ":" in digest else ""


def _clip(text: str, token_budget: int) -> str:
    limit = int(token_budget * CHARS_PER_TOKEN)
    return text if len(text) <= limit else text[:limit]


def _identity_section(persona: Persona) -> str:
    goals = "; ".join(
        f"{g.get('description')} (우선순위 {g.get('priority')})" for g in persona.goals
    )
    needs = ", ".join(f"{k}={v}" for k, v in sorted(persona.needs_bias.items()))
    traits = ", ".join(f"{k}={v}" for k, v in sorted(persona.big_five.items()))
    text = (
        f"당신은 '{persona.name}'({persona.id}) — {persona.archetype}.\n"
        f"{persona.identity_core}\n"
        f"성격(Big Five): {traits}\n"
        f"욕구 편향: {needs}\n"
        f"목표: {goals}\n"
        "당신은 살아있는 세계의 주민이다. 항상 이 인물로서 지각하고 결정한다."
    )
    return _clip(text, BUDGET_IDENTITY)


def _memory_split(memory_tokens: int) -> tuple[int, int]:
    """기억 예산을 (회상, 작업 기억)으로 나눈다 — 현행 비율(600:1200)을 유지한다.

    액터별 상한이 걸려도 두 기억의 균형은 그대로여야 한다: 한쪽만 줄이면
    '자원이 적은 인물'이 아니라 '한쪽 기억만 없는 인물'이 되어 다른 실험이 된다.
    """
    total = BUDGET_EPISODES + BUDGET_WORKING
    episodes = max(1, round(memory_tokens * BUDGET_EPISODES / total))
    return episodes, max(1, memory_tokens - episodes)


def _episodes_section(episodes: list[Recollection], budget: int) -> Section:
    """Episodes(4) — 장기 기억 회상 (ADR-008 recall). 비어 있으면 섹션 생략이 아니라
    고정 문구를 둔다 — 섹션 존재 자체가 프리픽스 안정성에 기여한다 (ADR-009/018).

    **예산에 잘려 나간 회상은 source_ids에도 없다** — 프롬프트에 실제로 들어간
    것만 근거다. 자른 뒤에 담는 이 순서가 그 계약이다 (ADR-021 §2).
    """
    if not episodes:
        return Section("episodes", "## 떠오르는 기억\n(지금 상황과 이어지는 오래된 기억은 없다)")
    limit = int(budget * CHARS_PER_TOKEN)
    kept: list[Recollection] = []
    used = 0
    for episode in episodes:
        if used + len(episode.text) > limit:
            break
        kept.append(episode)
        used += len(episode.text) + 1
    return Section(
        "episodes",
        "## 떠오르는 기억 (관련 회상)\n" + "\n".join(f"- {e.text}" for e in kept),
        tuple(e.event_id for e in kept if e.event_id),
    )


def _conversation_section(
    turns: list[tuple[str, str]], *, player_label: str = "관찰자", mark_last: bool = True
) -> str:
    """이 사람과의 대화를 시간순으로 (reply_to_player·proactive_dm, ADR-009).

    working이 최신순으로 뒤섞여 들어가면 LLM이 대화를 흐름으로 못 읽는다 —
    여기서 오래된→최근 순으로 펴 주고, 답장(reply_to_player)이면 마지막 상대
    발화에 답할 지점을 찍는다. 선제 DM은 답할 발화가 없다 — 표식 없이 흐름만.
    """
    if not turns:
        return ""
    last_player = max(
        (i for i, (who, _) in enumerate(turns) if who == "player"), default=-1
    ) if mark_last else -1
    lines: list[str] = []
    for i, (who, text) in enumerate(turns):
        label = "나" if who == "me" else player_label
        marker = "   ← 지금 이 말에 답하라" if i == last_player else ""
        lines.append(f"- {label}: {text}{marker}")
    return "## 이 사람과 나눈 대화 (오래된 순)\n" + "\n".join(lines)


def _working_section(entries: list[str], budget: int) -> str:
    """최근 우선 절단 (ADR-009 규칙 2) — 예산 안에서 최신 항목부터 담는다."""
    limit = int(budget * CHARS_PER_TOKEN)
    kept: list[str] = []
    used = 0
    for entry in entries:  # entries는 최신 우선
        if used + len(entry) > limit:
            break
        kept.append(entry)
        used += len(entry) + 1
    if not kept:
        return "## 작업 기억\n(최근 기억 없음 — 방금 깨어났다)"
    return "## 작업 기억 (최신 우선)\n" + "\n".join(f"- {e}" for e in kept)


def _seen_posts_section(posts: list[tuple[str, str]]) -> Section | None:
    """방금 피드에서 본 이웃의 글 (액터 소셜 루프) — 자발 댓글 결정의 재료.

    (post_event_id, 표시줄) 쌍을 받는다 — 본 글은 그 자체로 사건이라 출처가 있다.
    비어 있으면 섹션 생략 (아크·관계와 같은 규약 — 없는 것은 소음이다).
    댓글은 의무가 아니다 — 마음이 움직였을 때만, 그 결이 지침의 전부다.
    """
    if not posts:
        return None
    text = (
        "## 방금 피드에서 본 글\n"
        + "\n".join(f"- {line}" for _, line in posts)
        + "\n마음이 움직이면 comment 필드로 짧은 답글을 남겨도 좋다 — 의무가 아니다."
        "\n지금 감정을 숨기지 마라 — 반가우면 반갑게, 심드렁하면 남기지 않는 것도 답이다."
    )
    return Section("seen_posts", _clip(text, BUDGET_SEEN_POSTS), tuple(pid for pid, _ in posts))


def _world_section(world: WorldContext) -> str:
    text = (
        "## 세계 상황\n"
        f"- 세계: {world.world_id}, tick {world.tick}\n"
        f"- 세계 시간: {world.world_time.isoformat()}"
    )
    return _clip(text, BUDGET_WORLD)


_TASK_FRAMES = {
    "decide_action": (
        "## 임무\n"
        "지금 이 tick에서 당신이 할 행동 하나를 결정하라.\n"
        "- 행동은 성격·욕구·목표·작업 기억과 일관되어야 한다.\n"
        "- intent는 피드 내레이션에 쓰일 한 줄 요약이다 — 구체적이고 이 인물답게.\n"
        "- headline은 이 행동의 짧은 제목이다(20자 안팎) — 무엇을 하는지 한눈에,\n"
        "  틀에 박힌 말('일에 몰두하다') 말고 이 순간에 맞게. intent를 압축한 헤드라인.\n"
        "- target_actor_id는 실제로 존재하는 상대에게만. 없으면 null.\n"
        "- 출력은 지정된 JSON 스키마를 정확히 따른다. JSON 외 텍스트 금지."
    ),
    "post_status": (
        "## 임무\n"
        "지금은 SNS에 근황을 남기고 싶은 순간이다 — 의무가 아니라 문득 이는 충동이다.\n"
        "요즘의 일·기분·관심사에서 지금 나누고 싶은 것 하나를 골라 행동으로 옮겨라.\n"
        "- 작업 기억의 '지금 기분'이 지금의 너다 — 감정을 숨기지 말고 글의 결에\n"
        "  드러내라. 기쁘면 들뜨게, 화나면 날 서게, 지치면 지친 대로 (ADR-015).\n"
        "- 행동은 성격·욕구·목표·작업 기억과 일관되어야 한다.\n"
        "- intent는 피드 내레이션에 쓰일 한 줄 요약이다 — 구체적이고 이 인물답게.\n"
        "- headline은 이 행동의 짧은 제목이다(20자 안팎) — intent를 압축한 헤드라인.\n"
        "- target_actor_id는 실제로 존재하는 상대에게만. 없으면 null.\n"
        "- 출력은 지정된 JSON 스키마를 정확히 따른다. JSON 외 텍스트 금지."
    ),
    "follow_up_post": (
        "## 임무\n"
        "지난 대화 하나가 며칠째 마음에 남아 있다 — 작업 기억 맨 위 '마음에 남은 대화'가\n"
        "그것이다. 지금은 그 여운을 글로 옮기고 싶은 순간이다. 그 대화가 당신 안에 남긴\n"
        "것 — 생각의 변화, 다시 고른 답, 아직 맴도는 물음 — 을 근황 글로 남겨라.\n"
        "- 그 상대는 '그 사람'처럼 세계 안의 말로만 불러라 — 식별자·기계 표기 금지.\n"
        "- 작업 기억의 '지금 기분'이 지금의 너다 — 감정을 숨기지 말고 글의 결에 드러내라.\n"
        "- intent는 피드 내레이션에 쓰일 한 줄 요약이다 — 구체적이고 이 인물답게.\n"
        "- headline은 이 행동의 짧은 제목이다(20자 안팎) — intent를 압축한 헤드라인.\n"
        "- target_actor_id는 실제로 존재하는 상대에게만. 없으면 null (그 사람은 액터가 아니다).\n"
        "- 출력은 지정된 JSON 스키마를 정확히 따른다. JSON 외 텍스트 금지."
    ),
    "reply_to_player": (
        "## 임무\n"
        "위 '이 사람과 나눈 대화'가 지금까지의 흐름이다. 표시된 마지막 상대 발화에 답하라.\n"
        "- 흐름을 이어서 답한다: 상대가 물으면 실제로 답하고(예/아니오·구체적인 말),\n"
        "  같은 감사 인사('고마워')를 매번 반복하지 않는다.\n"
        "- 말투를 유지한다 — 당신이 앞서 반말이었으면 반말, 존댓말이었으면 존댓말.\n"
        "- 이 인물답게 1~3문장. 과장 금지 — 관계의 온도와 지금 상황에 맞게.\n"
        "- 상대는 세계 밖의 관찰자지만, 당신에게는 그냥 아는 사람이다.\n"
        '- 출력은 {"text": "..."} JSON 하나뿐이다. JSON 외 텍스트 금지.'
    ),
    "reply_to_comment": (
        "## 임무\n"
        "누군가 당신의 글에 댓글을 남겼다 — 작업 기억 최신 항목에 그 내용이 있다.\n"
        "그 사람에게 한 번, 짧게 답하라 (1~2문장). 이 인물답게, 관계의 온도에 맞게.\n"
        "- 지금 감정을 숨기지 말고 답의 결에 드러내라 — 기쁘면 들뜨게, 지치면 지친 대로.\n"
        "- 말투를 유지한다 — 평소 반말이면 반말, 존댓말이면 존댓말.\n"
        '- 출력은 {"text": "..."} JSON 하나뿐이다. JSON 외 텍스트 금지.'
    ),
    "greet_reaction": (
        "## 임무\n"
        "처음 보는 사람이 방금 당신 글에 좋아요를 남겼다 — 작업 기억 최신 항목에 있다.\n"
        "낯선 사람의 작은 호의를 알아차린 첫 인사를 그 글의 댓글로 남겨라.\n"
        "- 한두 문장, 과하지 않게 — 가벼운 고마움과 반가움 정도. 부담 주는 친밀함 금지.\n"
        "- 상대를 아는 척하지 마라 — 처음 마주친 사이다. 이 인물답게, 지금 감정의 결대로.\n"
        '- 출력은 {"text": "..."} JSON 하나뿐이다. JSON 외 텍스트 금지.'
    ),
    "proactive_dm": (
        "## 임무\n"
        "문득 이 사람이 생각났다 — 오랜만에 당신이 먼저 안부를 건네는 순간이다.\n"
        "위 '이 사람과 나눈 대화'와 떠오르는 기억이 함께 나눈 시간이다.\n"
        "그중 한 조각을 자연스럽게 언급하며 안부를 물어라.\n"
        "- 한두 문장, 부담 주지 않게 — 답을 요구하는 질문 공세 금지, 잔잔한 안부의 결.\n"
        "- 말투를 유지한다 — 평소 반말이면 반말, 존댓말이면 존댓말.\n"
        '- 출력은 {"text": "..."} JSON 하나뿐이다. JSON 외 텍스트 금지.'
    ),
    "reflect": (
        "## 임무\n"
        "위 기억들을 곱씹어, 지금 당신 안에 굳어진 생각 하나를 명제로 만들어라 —\n"
        "사건의 나열이 아니라 그 경험들이 '의미하는 것'이다 (ADR-008 reflection).\n"
        "- statement: 신념 한 줄. 이 인물의 목소리로, 구체적으로.\n"
        "- kind: self_image(나는 어떤 사람인가) / world_view(세상은 어떤 곳인가) /\n"
        "  person_insight(특정 인물에 대한 깨달음).\n"
        "- about_actor_id: person_insight면 반드시 '아는 사람들'의 id, 아니면 null.\n"
        "- confidence: 확신 0~1 — 근거가 반복될수록 높다. 억지 통찰이면 낮게.\n"
        "- 출력은 지정된 JSON 스키마를 정확히 따른다. JSON 외 텍스트 금지."
    ),
}


def build(
    persona: Persona,
    working: list[str],
    world: WorldContext,
    *,
    purpose: str = "decide_action",
    trace_id: str | None = None,
    episodes: list[Recollection] | None = None,
    conversation: list[tuple[str, str]] | None = None,
    arc: Arc | None = None,
    relationships: str | None = None,
    seen_posts: list[tuple[str, str]] | None = None,
    #: 기억 두 섹션(회상·작업)의 합산 예산 — 액터별 인지 예산이 여기로 들어온다
    #: (ADR-021 §3). 기본은 현행 상수의 합이라 주지 않으면 동작이 같다.
    memory_tokens: int = BUDGET_EPISODES + BUDGET_WORKING,
) -> Bundle:
    """ContextBundle 조립 — 섹션 순서 고정 (ADR-009 규칙 1: Relationship(3) <
    Episodes(4) < Working(5)).

    arc(있으면)가 최상위 프레임으로 앞에 온다 — 인생 방향이 이번 결정을 물들인다.
    relationships(있으면)는 아크 뒤·에피소드 앞 — 기억을 회상하기 전에 상대와의
    온도가 먼저 놓인다. conversation(reply_to_player 전용)이 있으면 Working 앞에
    대화 흐름 섹션을 끼운다 — 답장은 뒤섞인 버퍼가 아니라 시간순 대화를 보고 써야 한다.
    seen_posts(있으면)는 Working 뒤 — 방금 본 이웃의 글이 자발 댓글의 재료가 된다
    (액터 소셜 루프). 변동성이 가장 큰 섹션이라 프리픽스 캐시를 해치지 않는 뒤쪽이다.
    """
    frame = _TASK_FRAMES.get(purpose)
    if frame is None:
        raise ValueError(f"알 수 없는 purpose: {purpose}")
    identity = Section("identity", _identity_section(persona))
    sections: list[Section] = []
    if (arc_text := _arc_section(arc)):
        sections.append(Section("arc", arc_text))
    if (rel_text := _relationships_section(relationships)):
        sections.append(Section("relationships", rel_text))
    episode_budget, working_budget = _memory_split(memory_tokens)
    sections.append(_episodes_section(episodes or [], episode_budget))
    if conversation:
        sections.append(
            Section(
                "conversation",
                _conversation_section(conversation, mark_last=purpose == "reply_to_player"),
            )
        )
    sections.append(Section("working", _working_section(working, working_budget)))
    if (posts := _seen_posts_section(seen_posts or [])) is not None:
        sections.append(posts)
    sections += [
        Section("world", _world_section(world)),
        Section("task_frame", _clip(frame, BUDGET_TASK_FRAME)),
    ]
    return Bundle(
        purpose=purpose,
        system=identity.text,
        user="\n\n".join(s.text for s in sections),
        trace_id=trace_id or new_ulid(),
        # identity가 맨 앞이다 — 기록된 순서가 곧 프롬프트 순서여야 재조립이 성립한다
        sections=(identity, *sections),
    )
