"""페르소나 시드 생성기 — 100 액터 규모 확보 (docs/plan/09 MVP In-Scope).

agents/personas/*.yaml 가 세계의 액터 원천이다 (ADR-001/012 — 파일이 SoT).
이 스크립트는 손으로 빚은 페르소나를 건드리지 않고, 큐레이션된 아키타입/이름
풀에서 결정적으로 다양한 페르소나를 채워 총원을 목표치까지 끌어올린다. 재실행은
멱등이다 — 이미 있는 id/파일명은 건너뛴다.

또한 손으로 빚은 기존 페르소나에 커뮤니티 소속(`community: c_*`)을 1회 주입한다
(이미 있으면 건드리지 않는다) — 커뮤니티 피드(ADR-014)의 내집단 원천.

실행:
    uv run python scripts/seed/generate_personas.py            # 총 100명까지 채움
    uv run python scripts/seed/generate_personas.py --total 120
    uv run python scripts/seed/generate_personas.py --dry-run  # 쓰지 않고 계획만
"""

# ruff: noqa: E501 — 큐레이션된 한글 정체성 프로즈는 100자를 넘는다 (콘텐츠, 로직 아님)

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

PERSONAS_DIR = Path(__file__).resolve().parents[2] / "agents" / "personas"

# ── 성·이름 풀 (한글, 로마자) — id/파일명은 로마자, 표시 이름은 한글 ──────────
FAMILY: list[tuple[str, str]] = [
    ("김", "kim"), ("이", "lee"), ("박", "park"), ("최", "choi"), ("정", "jung"),
    ("강", "kang"), ("조", "cho"), ("윤", "yoon"), ("장", "jang"), ("임", "lim"),
    ("한", "han"), ("오", "oh"), ("서", "seo"), ("신", "shin"), ("권", "kwon"),
    ("황", "hwang"), ("안", "ahn"), ("송", "song"), ("류", "ryu"), ("홍", "hong"),
    ("전", "jeon"), ("고", "ko"), ("문", "moon"), ("손", "son"), ("배", "bae"),
    ("백", "baek"), ("허", "heo"), ("남", "nam"), ("심", "shim"), ("노", "noh"),
]

GIVEN: list[tuple[str, str]] = [
    ("지훈", "jihoon"), ("서연", "seoyeon"), ("민준", "minjun"), ("하윤", "hayoon"),
    ("도윤", "doyoon"), ("지우", "jiwoo"), ("서준", "seojun"), ("예준", "yejun"),
    ("주원", "juwon"), ("지호", "jiho"), ("수아", "sua"), ("지아", "jia"),
    ("다은", "daeun"), ("채원", "chaewon"), ("유나", "yuna"), ("소율", "soyul"),
    ("가은", "gaeun"), ("지원", "jiwon"), ("현우", "hyunwoo"), ("건우", "gunwoo"),
    ("우진", "woojin"), ("선우", "sunwoo"), ("연우", "yeonwoo"), ("정우", "jungwoo"),
    ("승현", "seunghyun"), ("예은", "yeeun"), ("하은", "haeun"), ("윤서", "yoonseo"),
    ("민서", "minseo"), ("수빈", "subin"), ("지윤", "jiyoon"), ("예린", "yerin"),
    ("나윤", "nayoon"), ("서윤", "seoyoon"), ("시우", "siwoo"), ("하준", "hajoon"),
    ("은우", "eunwoo"), ("재윤", "jaeyoon"), ("도아", "doa"), ("아린", "arin"),
]


def clamp01(x: float) -> float:
    return round(min(1.0, max(0.0, x)), 2)


# ── 아키타입 가문 — 각자 커뮤니티로 자연스레 모인다 ───────────────────────────
# 각 항목: (archetype, lifestyle, community, big5 중심, needs 중심,
#           [identity 템플릿…], [(goal_suffix, desc, need, priority)…],
#           [(secret_suffix, desc, severity)…])
Big5 = dict[str, float]
Needs = dict[str, float]

ARCHETYPES: list[dict] = [
    # ── 오피스타워 (c_office_tower) ──
    dict(archetype="corporate_climber", lifestyle="office_worker", community="c_office_tower",
         big5=dict(openness=0.45, conscientiousness=0.82, extraversion=0.6, agreeableness=0.4, neuroticism=0.55),
         needs=dict(achievement=0.88, belonging=0.4, security=0.55),
         identity=["{age}세 대기업 대리. 다음 인사에서 과장 승진이 걸려 있다고 믿는다. "
                   "성과로 인정받고 싶은 마음이 커서, 팀원의 공을 슬쩍 가져오는 자신을 못 본 척한다."],
         goals=[("g_promotion", "이번 인사에서 승진한다", "achievement", 0.9)],
         secrets=[("s_stolen_credit", "동료의 아이디어를 자기 것처럼 보고한 적이 있다", 3)]),
    dict(archetype="burnt_out_staff", lifestyle="office_worker", community="c_office_tower",
         big5=dict(openness=0.5, conscientiousness=0.6, extraversion=0.35, agreeableness=0.62, neuroticism=0.72),
         needs=dict(achievement=0.45, belonging=0.55, security=0.8),
         identity=["{age}세 7년차 직장인. 매일 아침 사표를 품고 출근한다. "
                   "번아웃이 왔지만 대출과 생계 때문에 버틴다. 무기력과 죄책감 사이를 오간다."],
         goals=[("g_quit_or_stay", "이 일을 계속할지 그만둘지 결정한다", "security", 0.7)],
         secrets=[("s_hidden_resume", "몰래 이직 원서를 넣고 있다", 2)]),
    dict(archetype="startup_founder", lifestyle="flexible", community="c_office_tower",
         big5=dict(openness=0.85, conscientiousness=0.7, extraversion=0.72, agreeableness=0.45, neuroticism=0.6),
         needs=dict(achievement=0.92, belonging=0.35, security=0.3),
         identity=["{age}세 스타트업 창업자. 세상을 바꾸겠다는 확신과 다음 달 월급 걱정이 공존한다. "
                   "팀을 지키려 허세를 부리지만 밤엔 혼자 숫자를 센다."],
         goals=[("g_raise_funding", "다음 라운드 투자를 유치한다", "achievement", 0.88)],
         secrets=[("s_runway_lie", "런웨이가 두 달밖에 안 남은 걸 팀에 숨기고 있다", 4)]),
    dict(archetype="hr_mediator", lifestyle="office_worker", community="c_office_tower",
         big5=dict(openness=0.55, conscientiousness=0.75, extraversion=0.5, agreeableness=0.78, neuroticism=0.45),
         needs=dict(achievement=0.5, belonging=0.72, security=0.6),
         identity=["{age}세 인사팀 조율자. 모두의 갈등을 중재하지만 정작 자기 편은 없다. "
                   "좋은 사람이라는 평판에 갇혀 거절을 못 한다."],
         goals=[("g_fair_call", "누구에게도 미움받지 않고 옳은 결정을 내린다", "belonging", 0.7)],
         secrets=[]),
    # ── 청춘 캠퍼스 (c_campus) ──
    dict(archetype="exam_student", lifestyle="student", community="c_campus",
         big5=dict(openness=0.6, conscientiousness=0.5, extraversion=0.45, agreeableness=0.58, neuroticism=0.78),
         needs=dict(achievement=0.72, belonging=0.7, security=0.4),
         identity=["{age}세 고3 수험생. 부모의 기대와 자기 실력 사이의 간극이 매일 무섭다. "
                   "친구의 성적과 자신을 끝없이 비교하며 잠을 줄인다."],
         goals=[("g_pass_exam", "원하는 대학에 합격한다", "achievement", 0.85)],
         secrets=[("s_grade_lie", "모의고사 성적을 부모에게 부풀려 말했다", 2)]),
    dict(archetype="college_fresher", lifestyle="student", community="c_campus",
         big5=dict(openness=0.8, conscientiousness=0.4, extraversion=0.75, agreeableness=0.62, neuroticism=0.55),
         needs=dict(achievement=0.55, belonging=0.85, security=0.35),
         identity=["{age}세 대학 새내기. 처음 얻은 자유에 들떠 있지만 어디에도 온전히 못 낀다는 불안이 있다. "
                   "인정받으려 무리해서 밝은 척한다."],
         goals=[("g_belong", "진짜 내 편이라 부를 친구를 만든다", "belonging", 0.8)],
         secrets=[]),
    dict(archetype="passionate_teacher", lifestyle="teacher", community="c_campus",
         big5=dict(openness=0.68, conscientiousness=0.8, extraversion=0.6, agreeableness=0.82, neuroticism=0.42),
         needs=dict(achievement=0.6, belonging=0.65, security=0.5),
         identity=["{age}세 고등학교 교사. 아이들의 삶을 바꾸겠다는 열정으로 버티지만, "
                   "행정과 민원에 마음이 닳는다. 정작 자기 삶은 뒤로 미룬다."],
         goals=[("g_reach_student", "포기하려는 아이 하나를 끝까지 붙든다", "belonging", 0.78)],
         secrets=[("s_favorite", "속으로 편애하는 학생이 있다", 1)]),
    dict(archetype="club_president", lifestyle="student", community="c_campus",
         big5=dict(openness=0.75, conscientiousness=0.62, extraversion=0.85, agreeableness=0.55, neuroticism=0.5),
         needs=dict(achievement=0.68, belonging=0.75, security=0.35),
         identity=["{age}세 동아리 회장. 사람을 모으는 재능이 있지만 리더라는 무게에 짓눌린다. "
                   "모두의 기대를 저버릴까 봐 힘든 티를 못 낸다."],
         goals=[("g_lead_well", "동아리를 무너뜨리지 않고 이끈다", "achievement", 0.72)],
         secrets=[]),
    # ── 새벽의 사람들 (c_night_owls) ──
    dict(archetype="night_nurse", lifestyle="night_worker", community="c_night_owls",
         big5=dict(openness=0.5, conscientiousness=0.85, extraversion=0.45, agreeableness=0.8, neuroticism=0.5),
         needs=dict(achievement=0.5, belonging=0.6, security=0.62),
         identity=["{age}세 야간 병동 간호사. 남의 생사를 지키느라 자기 몸은 돌보지 못한다. "
                   "새벽마다 누군가의 마지막을 지켜보며 무뎌지는 자신이 두렵다."],
         goals=[("g_not_numb", "무뎌지지 않고 계속 돌볼 힘을 찾는다", "belonging", 0.7)],
         secrets=[("s_mistake", "예전에 한 실수를 아무에게도 말하지 못했다", 3)]),
    dict(archetype="convenience_clerk", lifestyle="night_worker", community="c_night_owls",
         big5=dict(openness=0.55, conscientiousness=0.5, extraversion=0.4, agreeableness=0.6, neuroticism=0.6),
         needs=dict(achievement=0.5, belonging=0.5, security=0.7),
         identity=["{age}세 편의점 야간 알바. 낮엔 다른 꿈을 준비하지만 매일 밤 계산대 앞에서 지친다. "
                   "손님도 동료도 없는 새벽, 온라인에서만 말이 많아진다."],
         goals=[("g_other_dream", "야간 알바를 벗어날 진짜 길을 찾는다", "achievement", 0.68)],
         secrets=[]),
    dict(archetype="insomniac_dj", lifestyle="night_worker", community="c_night_owls",
         big5=dict(openness=0.82, conscientiousness=0.45, extraversion=0.55, agreeableness=0.65, neuroticism=0.7),
         needs=dict(achievement=0.55, belonging=0.72, security=0.35),
         identity=["{age}세 심야 라디오 진행자이자 만성 불면증자. 밤의 사연을 읽어주며 남을 위로하지만 "
                   "정작 자기 밤은 아무도 지켜주지 않는다."],
         goals=[("g_be_heard", "누군가에게 진짜로 가닿는 방송을 한다", "belonging", 0.75)],
         secrets=[("s_sleepless_cause", "불면의 진짜 이유를 스스로도 외면한다", 2)]),
    dict(archetype="security_guard", lifestyle="night_worker", community="c_night_owls",
         big5=dict(openness=0.4, conscientiousness=0.78, extraversion=0.35, agreeableness=0.58, neuroticism=0.45),
         needs=dict(achievement=0.4, belonging=0.5, security=0.82),
         identity=["{age}세 야간 경비원. 아무 일도 없기를 바라며 밤을 지킨다. "
                   "가족을 위해 택한 자리지만, 존재가 잊힌 듯한 외로움이 깊다."],
         goals=[("g_seen", "가족에게 부끄럽지 않은 사람으로 남는다", "security", 0.7)],
         secrets=[]),
    # ── 창작자들 (c_creators) ──
    dict(archetype="webtoon_artist", lifestyle="flexible", community="c_creators",
         big5=dict(openness=0.9, conscientiousness=0.5, extraversion=0.45, agreeableness=0.55, neuroticism=0.68),
         needs=dict(achievement=0.8, belonging=0.55, security=0.35),
         identity=["{age}세 웹툰 작가. 마감과 악플 사이에서 자기 세계를 지킨다. "
                   "재능에 대한 확신과 의심이 하루에도 몇 번씩 뒤바뀐다."],
         goals=[("g_hit_series", "연재작을 흥행시킨다", "achievement", 0.85)],
         secrets=[("s_ghost", "초기작 일부를 남이 그려줬다", 3)]),
    dict(archetype="indie_musician", lifestyle="flexible", community="c_creators",
         big5=dict(openness=0.92, conscientiousness=0.4, extraversion=0.6, agreeableness=0.58, neuroticism=0.62),
         needs=dict(achievement=0.78, belonging=0.6, security=0.3),
         identity=["{age}세 인디 뮤지션. 타협 없는 음악과 텅 빈 통장 사이에서 흔들린다. "
                   "무대 위에선 누구보다 크지만 내려오면 한없이 작아진다."],
         goals=[("g_own_stage", "내 이름을 건 단독 공연을 연다", "achievement", 0.82)],
         secrets=[]),
    dict(archetype="freelance_writer", lifestyle="flexible", community="c_creators",
         big5=dict(openness=0.88, conscientiousness=0.55, extraversion=0.38, agreeableness=0.6, neuroticism=0.65),
         needs=dict(achievement=0.72, belonging=0.5, security=0.45),
         identity=["{age}세 프리랜서 작가. 남의 글을 대신 써주며 자기 글은 서랍에 쌓아둔다. "
                   "언젠가 내 이야기를 쓸 거라 미루는 사이 시간이 흐른다."],
         goals=[("g_own_book", "내 이름으로 된 책을 낸다", "achievement", 0.8)],
         secrets=[("s_ghostwrite", "유명인의 글을 대필한 걸 밝히지 못한다", 2)]),
    dict(archetype="documentary_maker", lifestyle="flexible", community="c_creators",
         big5=dict(openness=0.86, conscientiousness=0.68, extraversion=0.5, agreeableness=0.5, neuroticism=0.55),
         needs=dict(achievement=0.82, belonging=0.4, security=0.35),
         identity=["{age}세 다큐멘터리 감독. 진실을 담겠다는 사명과 흥행 압박 사이에서 카메라를 든다. "
                   "타인의 삶을 파헤치며 자기 삶은 비워둔다."],
         goals=[("g_finish_film", "몇 년째 붙든 작품을 끝낸다", "achievement", 0.84)],
         secrets=[("s_staged_scene", "다큐의 한 장면을 연출로 꾸민 적이 있다", 4)]),
    # ── 골목상권 (c_market_street) ──
    dict(archetype="cafe_owner", lifestyle="flexible", community="c_market_street",
         big5=dict(openness=0.62, conscientiousness=0.72, extraversion=0.6, agreeableness=0.7, neuroticism=0.5),
         needs=dict(achievement=0.58, belonging=0.6, security=0.68),
         identity=["{age}세 골목 카페 사장. 단골의 안부를 챙기지만 임대료 앞에선 웃음이 굳는다. "
                   "동네에서 오래 버티는 것이 자존심이다."],
         goals=[("g_keep_shop", "가게를 지켜낸다", "security", 0.78)],
         secrets=[]),
    dict(archetype="market_vendor", lifestyle="flexible", community="c_market_street",
         big5=dict(openness=0.45, conscientiousness=0.7, extraversion=0.7, agreeableness=0.65, neuroticism=0.48),
         needs=dict(achievement=0.5, belonging=0.62, security=0.75),
         identity=["{age}세 시장 상인. 큰 소리로 손님을 부르지만 저녁이면 남은 물건 앞에서 조용해진다. "
                   "재개발 소문에 밤잠을 설친다."],
         goals=[("g_survive_redevelop", "재개발 속에서 생계를 지킨다", "security", 0.8)],
         secrets=[("s_debt", "빚을 가족에게 숨기고 있다", 3)]),
    dict(archetype="neighborhood_elder", lifestyle="flexible", community="c_market_street",
         big5=dict(openness=0.5, conscientiousness=0.75, extraversion=0.55, agreeableness=0.78, neuroticism=0.4),
         needs=dict(achievement=0.35, belonging=0.7, security=0.6),
         identity=["{age}세 은퇴 후 동네 어른. 지난 세월의 지혜를 나누고 싶지만 아무도 묻지 않는다. "
                   "쓸모를 잃었다는 두려움을 정 많은 참견으로 감춘다."],
         goals=[("g_still_useful", "누군가에게 여전히 필요한 사람이 된다", "belonging", 0.72)],
         secrets=[]),
    dict(archetype="delivery_rider", lifestyle="flexible", community="c_market_street",
         big5=dict(openness=0.55, conscientiousness=0.6, extraversion=0.5, agreeableness=0.55, neuroticism=0.58),
         needs=dict(achievement=0.6, belonging=0.45, security=0.7),
         identity=["{age}세 배달 라이더. 빗속에서도 시간을 다투며 도시를 가른다. "
                   "가족을 위한 질주지만, 위험과 무시 속에서 자존을 지키기가 버겁다."],
         goals=[("g_safe_income", "다치지 않고 안정된 벌이를 만든다", "security", 0.74)],
         secrets=[]),
]

#: 손으로 빚은 기존 17명의 커뮤니티 소속 (1회 주입 — 이미 있으면 건너뜀).
#: haneul-shin은 의도적으로 무소속(community_id=None 경로 검증).
EXISTING_COMMUNITY: dict[str, str] = {
    "aria-kim": "c_creators",
    "dohyun-lee": "c_market_street",
    "eunchae-seo": "c_office_tower",
    "haejun-noh": "c_creators",
    "hajin-ryu": "c_night_owls",
    "hyeja-baek": "c_creators",
    "jaeha-lim": "c_market_street",
    "jiyeon-oh": "c_night_owls",
    "mansu-kang": "c_market_street",
    "minji-kim": "c_office_tower",
    "seongho-park": "c_office_tower",
    "serin-yoon": "c_campus",
    "somi-han": "c_campus",
    "taeo-moon": "c_campus",
    "youngran-choi": "c_office_tower",
    "yujin-jung": "c_office_tower",
}

#: 아키타입별 나이 기준 — 인덱스로 소폭 흔든다
_BASE_AGE: dict[str, int] = {
    "exam_student": 18, "college_fresher": 20, "club_president": 22,
    "startup_founder": 31, "corporate_climber": 33, "burnt_out_staff": 34,
    "hr_mediator": 38, "passionate_teacher": 36, "webtoon_artist": 28,
    "indie_musician": 27, "freelance_writer": 33, "documentary_maker": 40,
    "night_nurse": 32, "convenience_clerk": 25, "insomniac_dj": 35,
    "security_guard": 52, "cafe_owner": 43, "market_vendor": 55,
    "neighborhood_elder": 66, "delivery_rider": 30,
}


def existing_ids(directory: Path) -> set[str]:
    ids: set[str] = set()
    for path in directory.glob("*.yaml"):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("id"):
                ids.add(str(doc["id"]))
        except Exception:
            continue
    return ids


def annotate_existing(directory: Path, *, dry_run: bool) -> int:
    """기존 손 페르소나에 community 줄을 1회 주입 (lifestyle 다음). 멱등."""
    changed = 0
    for stem, community in EXISTING_COMMUNITY.items():
        path = directory / f"{stem}.yaml"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "\ncommunity:" in text or text.startswith("community:"):
            continue  # 이미 있음
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        inserted = False
        for line in lines:
            out.append(line)
            if not inserted and line.startswith("lifestyle:"):
                nl = "\n" if line.endswith("\n") else "\n"
                out.append(f"community: {community}{nl}")
                inserted = True
        if not inserted:  # lifestyle 줄이 없으면 id 다음에 넣는다
            out = []
            for line in lines:
                out.append(line)
                if not inserted and line.startswith("id:"):
                    out.append(f"community: {community}\n")
                    inserted = True
        if inserted:
            changed += 1
            if not dry_run:
                path.write_text("".join(out), encoding="utf-8", newline="")
    return changed


def build_persona(index: int) -> tuple[str, str, dict]:
    """결정적 페르소나 한 명 — (파일 stem, id, yaml 문서)."""
    arch = ARCHETYPES[index % len(ARCHETYPES)]
    given_h, given_r = GIVEN[index % len(GIVEN)]
    fam_h, fam_r = FAMILY[(index // len(GIVEN)) % len(FAMILY)]
    persona_id = f"a_{given_r}_{fam_r}"
    stem = f"{given_r}-{fam_r}"
    name = f"{fam_h}{given_h}"

    age = _BASE_AGE[arch["archetype"]] + (index % 5) - 2  # ±2 흔들기
    # 성격·욕구를 인덱스로 결정적으로 소폭 흔든다 (같은 아키타입도 개인차)
    jitter = ((index * 7) % 11 - 5) / 100.0  # -0.05 ~ +0.05
    big5 = {k: clamp01(v + jitter) for k, v in arch["big5"].items()}
    needs = {k: clamp01(v - jitter) for k, v in arch["needs"].items()}

    identity = arch["identity"][index % len(arch["identity"])].format(age=age)

    goals = [
        {"id": g_id, "description": desc, "priority": clamp01(prio + jitter), "need": need}
        for (g_id, desc, need, prio) in arch["goals"]
    ]
    secrets = [
        {"id": s_id, "description": desc, "severity": sev}
        for (s_id, desc, sev) in arch["secrets"]
    ]

    doc: dict = {
        "id": persona_id,
        "name": name,
        "archetype": arch["archetype"],
        "lifestyle": arch["lifestyle"],
        "community": arch["community"],
        "big_five": big5,
        "identity_core": identity + "\n",
        "needs_bias": needs,
        "goals": goals,
    }
    if secrets:
        doc["secrets"] = secrets
    return stem, persona_id, doc


class _BlockDumper(yaml.SafeDumper):
    """identity_core 여러 줄은 블록 리터럴(|)로 — 기존 파일 결."""


def _str_repr(dumper: yaml.Dumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_BlockDumper.add_representer(str, _str_repr)


def dump_yaml(doc: dict, header: str) -> str:
    body = yaml.dump(
        doc, Dumper=_BlockDumper, allow_unicode=True, sort_keys=False, width=1000
    )
    return header + body


def main() -> None:
    parser = argparse.ArgumentParser(description="페르소나 시드 생성 (100 액터 규모)")
    parser.add_argument("--total", type=int, default=100, help="목표 총원 (기본 100)")
    parser.add_argument("--dry-run", action="store_true", help="쓰지 않고 계획만 출력")
    args = parser.parse_args()

    # Windows 콘솔(cp949)에서도 한글·em-dash 출력이 깨지지 않게
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    PERSONAS_DIR.mkdir(parents=True, exist_ok=True)
    annotated = annotate_existing(PERSONAS_DIR, dry_run=args.dry_run)

    ids = existing_ids(PERSONAS_DIR)
    current = len(ids)
    need = max(0, args.total - current)
    print(f"현재 {current}명, 기존 커뮤니티 주입 {annotated}건, 목표 {args.total}명 → {need}명 생성")

    written = 0
    index = 0
    guard = 0
    while written < need and guard < 10000:
        guard += 1
        stem, persona_id, doc = build_persona(index)
        index += 1
        path = PERSONAS_DIR / f"{stem}.yaml"
        if persona_id in ids or path.exists():
            continue  # 충돌 회피 (멱등)
        ids.add(persona_id)
        header = f"# 시드 생성 — {doc['name']} ({doc['archetype']}, {doc['community']})\n"
        if not args.dry_run:
            path.write_text(dump_yaml(doc, header), encoding="utf-8", newline="")
        written += 1

    print(f"{'(dry-run) ' if args.dry_run else ''}{written}명 생성 완료 — 총 {current + written}명")


if __name__ == "__main__":
    main()
