"""페르소나 스튜디오 관리 API — 파일(agents/personas, SoT)의 CRUD 중재.

조회·저장은 es에 아무것도 적재하지 않는다 — 세계 반영은 tick 워커 핫 리로드의
몫. 예외는 은퇴(DELETE)로, actor.identity.retired를 es에 적재한다(아래 은퇴 절).
전부 tmp_path 페르소나 디렉터리 — 인프라(NATS/PG/Redis) 없이 돈다
(lifespan을 태우지 않는 ASGITransport). 은퇴의 es 실적재만 PG 통합 테스트가
따로 본다(미설정 skip — conftest 가드).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from lf_gateway import admin
from lf_gateway.config import Config
from lf_gateway.main import create_app

ZED_YAML = """\
# 스튜디오 태생 — 저자성·비표준 필드(severity) 보존 검증용
id: a_zed
name: 제드
archetype: night_owl
lifestyle: night_worker
active: true
created_by: p_creator

big_five:
  openness: 0.7
  conscientiousness: 0.4
  extraversion: 0.6
  agreeableness: 0.5
  neuroticism: 0.3

identity_core: |
  밤에 일하는 보안 엔지니어. 낮의 세계와 어긋난 시차를 산다.

needs_bias:
  achievement: 0.6
  belonging: 0.5
  security: 0.7

goals:
  - id: g_ship_tool
    description: 자작 보안 도구를 공개한다
    priority: 0.8
    need: achievement

secrets:
  - id: s_burnout
    description: 사실 번아웃 직전이다
    severity: 3
"""

ARI_YAML = """\
id: a_ari
name: 아리
archetype: street_artist
lifestyle: flexible

big_five:
  openness: 0.9
  conscientiousness: 0.3
  extraversion: 0.8
  agreeableness: 0.6
  neuroticism: 0.4

identity_core: 골목 벽화를 그리는 화가.

needs_bias:
  achievement: 0.5
  belonging: 0.7
  security: 0.2

goals: []
secrets: []
"""


@pytest.fixture
def personas_dir(tmp_path: Path) -> Path:
    (tmp_path / "ari.yaml").write_text(ARI_YAML, encoding="utf-8")
    (tmp_path / "zed.yaml").write_text(ZED_YAML, encoding="utf-8")
    return tmp_path


def make_client(
    personas_dir: Path,
    *,
    token: str | None = None,
    # 기본은 닿지 않는 DSN — 대역을 안 붙인 테스트가 es를 건드리면 시끄럽게 죽는다
    pg_dsn: str = "postgresql://unused:unused@localhost:1/unused",
) -> httpx.AsyncClient:
    cfg = Config(
        nats_url="nats://localhost:4222", env="test",
        personas_dir=personas_dir, admin_token=token, pg_dsn=pg_dsn,
    )
    app = create_app(cfg)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── GET — 목록(파일명 순)·단건·404 ───────────────────────────────────────────


async def test_list_personas_in_filename_order(personas_dir):
    async with make_client(personas_dir) as client:
        resp = await client.get("/admin/personas")
    assert resp.status_code == 200
    docs = resp.json()["personas"]
    assert [d["id"] for d in docs] == ["a_ari", "a_zed"]  # ari.yaml < zed.yaml

    ari, zed = docs
    # 기존 yaml(active/created_by 없음)도 완전한 PersonaDoc으로 나온다 (하위 호환)
    assert ari["active"] is True and ari["created_by"] is None
    assert zed["active"] is True and zed["created_by"] == "p_creator"
    assert zed["lifestyle"] == "night_worker"
    assert zed["big_five"] == {
        "openness": 0.7, "conscientiousness": 0.4, "extraversion": 0.6,
        "agreeableness": 0.5, "neuroticism": 0.3,
    }
    assert zed["needs_bias"] == {"achievement": 0.6, "belonging": 0.5, "security": 0.7}
    assert zed["goals"] == [
        {"id": "g_ship_tool", "description": "자작 보안 도구를 공개한다",
         "priority": 0.8, "need": "achievement"},
    ]
    # secrets는 {id, description}만 노출한다 — severity 등 내부 결은 파일의 몫
    assert zed["secrets"] == [{"id": "s_burnout", "description": "사실 번아웃 직전이다"}]
    assert zed["identity_core"] == "밤에 일하는 보안 엔지니어. 낮의 세계와 어긋난 시차를 산다."


async def test_get_persona_by_id(personas_dir):
    async with make_client(personas_dir) as client:
        resp = await client.get("/admin/personas/a_zed")
    assert resp.status_code == 200
    assert resp.json()["id"] == "a_zed"


async def test_get_unknown_persona_is_404(personas_dir):
    async with make_client(personas_dir) as client:
        resp = await client.get("/admin/personas/a_nobody")
    assert resp.status_code == 404
    assert "detail" in resp.json()


# ── PUT — 검증 후 yaml 저장, 파일이 SoT (es 적재 없음) ───────────────────────


def valid_doc(persona_id: str = "a_new_face", **overrides) -> dict:
    doc = {
        "id": persona_id,
        "name": "새 인물",
        "archetype": "wanderer",
        "lifestyle": "student",
        "active": True,
        "created_by": None,
        "big_five": {
            "openness": 0.5, "conscientiousness": 0.5, "extraversion": 0.5,
            "agreeableness": 0.5, "neuroticism": 0.5,
        },
        "needs_bias": {"achievement": 0.4, "belonging": 0.5, "security": 0.6},
        "goals": [
            {"id": "g_settle", "description": "이 도시에 자리를 잡는다",
             "priority": 0.7, "need": "security"},
        ],
        "secrets": [{"id": "s_past", "description": "떠나온 이유를 말하지 않는다"}],
        "identity_core": "떠돌다 이 도시에 막 도착한 사람.",
    }
    doc.update(overrides)
    return doc


async def test_put_updates_existing_file_preserving_grain(personas_dir):
    async with make_client(personas_dir) as client:
        zed = (await client.get("/admin/personas/a_zed")).json()
        zed["name"] = "제드2"
        zed["big_five"]["openness"] = 1.5  # 범위 밖 — 0..1 클램프
        resp = await client.put("/admin/personas/a_zed", json=zed)

    assert resp.status_code == 200
    saved = resp.json()
    assert saved["name"] == "제드2"
    assert saved["big_five"]["openness"] == 1.0

    # 기존 파일은 id 스캔으로 위치한다 — 파일명 유지, 새 파일 없음
    path = personas_dir / "zed.yaml"
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # BOM 없이
    assert b"\r" not in raw  # LF — CRLF로 번역하지 않는다
    text = raw.decode("utf-8")
    assert text.startswith("# 스튜디오 태생 —")  # 첫 줄 주석 보존

    on_disk = load_yaml(path)
    assert on_disk["name"] == "제드2"
    assert on_disk["created_by"] == "p_creator"
    # PersonaDoc이 모르는 결(severity)은 지워지지 않는다 — 파일이 SoT
    assert on_disk["secrets"] == [
        {"id": "s_burnout", "description": "사실 번아웃 직전이다", "severity": 3},
    ]


async def test_put_creates_new_file_with_studio_comment(personas_dir):
    doc = valid_doc("a_new_face", created_by="p_maker")
    async with make_client(personas_dir) as client:
        resp = await client.put("/admin/personas/a_new_face", json=doc)
        listed = (await client.get("/admin/personas")).json()["personas"]

    assert resp.status_code == 200
    assert resp.json()["created_by"] == "p_maker"  # 생성 시에는 세팅 가능

    # 신규 파일명 — a_ 접두 제거·언더스코어→하이픈, 스튜디오 주석 한 줄
    path = personas_dir / "new-face.yaml"
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "# 새 인물 — Persona Studio"
    on_disk = load_yaml(path)
    assert on_disk["id"] == "a_new_face"
    assert on_disk["created_by"] == "p_maker"
    assert [d["id"] for d in listed] == ["a_ari", "a_new_face", "a_zed"]


async def test_put_deactivation_round_trips(personas_dir):
    # 휴면 스위치 — 스튜디오가 끄면 파일에 active: false가 남는다 (리로드가 재운다)
    async with make_client(personas_dir) as client:
        ari = (await client.get("/admin/personas/a_ari")).json()
        ari["active"] = False
        resp = await client.put("/admin/personas/a_ari", json=ari)
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert load_yaml(personas_dir / "ari.yaml")["active"] is False


async def test_created_by_is_immutable_after_creation(personas_dir):
    async with make_client(personas_dir) as client:
        # 수정으로 지울 수 없다
        zed = (await client.get("/admin/personas/a_zed")).json()
        zed["created_by"] = None
        erased = await client.put("/admin/personas/a_zed", json=zed)
        # 수정으로 바꿀 수도 없다
        zed["created_by"] = "p_thief"
        stolen = await client.put("/admin/personas/a_zed", json=zed)
        # 시스템 태생에 뒤늦게 저자를 붙일 수도 없다
        ari = (await client.get("/admin/personas/a_ari")).json()
        ari["created_by"] = "p_late"
        late = await client.put("/admin/personas/a_ari", json=ari)

    assert erased.json()["created_by"] == "p_creator"
    assert stolen.json()["created_by"] == "p_creator"
    assert load_yaml(personas_dir / "zed.yaml")["created_by"] == "p_creator"
    assert late.json()["created_by"] is None
    assert "created_by" not in load_yaml(personas_dir / "ari.yaml")


async def test_put_id_mismatch_is_422(personas_dir):
    async with make_client(personas_dir) as client:
        resp = await client.put("/admin/personas/a_zed", json=valid_doc("a_other"))
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "a_Bad-Id"},  # id 패턴 위반
        {"name": "   "},  # 이름 공백
        {"lifestyle": "vampire"},  # 닫힌 어휘 밖
        {"big_five": {"openness": 0.5}},  # 키 불완전
        {"needs_bias": {"achievement": 0.4, "belonging": 0.5}},  # 키 불완전
        {"goals": [{"id": "goal_1", "description": "x", "priority": 0.5,
                    "need": "achievement"}]},  # goal id 접두 위반
        {"goals": [{"id": "g_x", "description": "x", "priority": 0.5,
                    "need": "fame"}]},  # need 어휘 밖
        {"secrets": [{"id": "sec_1", "description": "x"}]},  # secret id 접두 위반
        {"created_by": "maker"},  # 저자 id 패턴 위반
    ],
)
async def test_put_validation_rejects_bad_docs(personas_dir, overrides):
    doc = valid_doc("a_new_face", **overrides)
    async with make_client(personas_dir) as client:
        resp = await client.put("/admin/personas/" + doc["id"], json=doc)
    assert resp.status_code == 422


async def test_put_clamps_scores_into_unit_range(personas_dir):
    doc = valid_doc(
        "a_new_face",
        big_five={"openness": -0.2, "conscientiousness": 2.0, "extraversion": 0.5,
                  "agreeableness": 0.5, "neuroticism": 0.5},
        goals=[{"id": "g_x", "description": "x", "priority": 1.7, "need": "achievement"}],
    )
    async with make_client(personas_dir) as client:
        resp = await client.put("/admin/personas/a_new_face", json=doc)
    saved = resp.json()
    assert saved["big_five"]["openness"] == 0.0
    assert saved["big_five"]["conscientiousness"] == 1.0
    assert saved["goals"][0]["priority"] == 1.0
    on_disk = load_yaml(personas_dir / "new-face.yaml")
    assert on_disk["big_five"]["openness"] == 0.0


# ── 저장은 수기 주석을 지우지 않는다 — yaml이 SoT, 주석이 곧 설계 기록 ────────

MUSE_YAML = """\
# 합성 페르소나 — 주석 보존 검증용 (수기 설계 기록의 대역)
# 구조: identity + OCEAN + needs/goals + secrets
id: a_muse
name: 뮤즈
archetype: test_subject
lifestyle: flexible
active: true

big_five:              # → baseline PAD 파생 (ADR-015)
  openness: 0.82
  conscientiousness: 0.55
  extraversion: 0.71
  agreeableness: 0.38
  neuroticism: 0.64

identity_core: |
  주석 보존을 시험하기 위해 태어난 합성 인물.
  두 번째 줄도 블록 그대로 남아야 한다.

needs_bias:            # 욕구 가중 (docs/plan/06)
  achievement: 0.90
  belonging: 0.40
  security: 0.30

goals:                 # need = 이 목표가 걸린 욕구 축
  - id: g_first
    description: 첫 목표
    priority: 0.9
    need: achievement
  - id: g_second        # 형제 항목 주석 — 교체에서 살아남아야 한다
    description: 둘째 목표
    priority: 0.5
    need: belonging

secrets:
  - id: s_hidden
    description: 감춰둔 것
    severity: 4          # 1(사소) ~ 5(파멸적)
"""


@pytest.fixture
def muse_dir(tmp_path: Path) -> Path:
    (tmp_path / "muse.yaml").write_text(MUSE_YAML, encoding="utf-8")
    return tmp_path


async def test_put_noop_leaves_file_byte_identical(muse_dir):
    # 아무것도 바꾸지 않은 저장은 파일을 훼손할 권리가 없다 — 왕복의 기준선
    async with make_client(muse_dir) as client:
        muse = (await client.get("/admin/personas/a_muse")).json()
        resp = await client.put("/admin/personas/a_muse", json=muse)
    assert resp.status_code == 200
    assert (muse_dir / "muse.yaml").read_text(encoding="utf-8") == MUSE_YAML


async def test_put_preserves_comments_order_and_block_scalar(muse_dir):
    async with make_client(muse_dir) as client:
        muse = (await client.get("/admin/personas/a_muse")).json()
        muse["name"] = "뮤즈2"
        muse["big_five"]["openness"] = 0.7  # 중첩 갱신
        resp = await client.put("/admin/personas/a_muse", json=muse)
        reread = (await client.get("/admin/personas/a_muse")).json()

    assert resp.status_code == 200
    path = muse_dir / "muse.yaml"
    text = path.read_text(encoding="utf-8")

    # ① 값 갱신
    on_disk = load_yaml(path)
    assert on_disk["name"] == "뮤즈2"
    assert on_disk["big_five"]["openness"] == 0.7

    # ② 주석 생존 — 머리 주석·키 옆 주석(칸 맞춤 포함)·중첩 항목 주석
    assert text.startswith(
        "# 합성 페르소나 — 주석 보존 검증용 (수기 설계 기록의 대역)\n"
        "# 구조: identity + OCEAN + needs/goals + secrets\n"
    )
    assert "big_five:              # → baseline PAD 파생 (ADR-015)" in text
    assert "needs_bias:            # 욕구 가중 (docs/plan/06)" in text
    assert "goals:                 # need = 이 목표가 걸린 욕구 축" in text
    assert "severity: 4          # 1(사소) ~ 5(파멸적)" in text

    # ② 키 순서 생존 (safe_load dict 순서 = 파일 순서)
    assert list(on_disk) == [
        "id", "name", "archetype", "lifestyle", "active",
        "big_five", "identity_core", "needs_bias", "goals", "secrets",
    ]

    # ③ 블록 스칼라 스타일 유지
    assert "identity_core: |" in text
    assert "두 번째 줄도 블록 그대로 남아야 한다." in text

    # ④ 재로드 시 파싱 동일 — API가 돌려주는 값과 파일이 일치
    assert reread["name"] == "뮤즈2"
    assert reread["big_five"]["openness"] == 0.7
    assert reread["identity_core"] == (
        "주석 보존을 시험하기 위해 태어난 합성 인물.\n두 번째 줄도 블록 그대로 남아야 한다."
    )


async def test_put_goal_removal_keeps_sibling_comments(muse_dir):
    # 항목 삭제 — 지운 항목만 사라지고, 남은 형제의 주석과 파일 결은 그대로
    async with make_client(muse_dir) as client:
        muse = (await client.get("/admin/personas/a_muse")).json()
        muse["goals"] = [g for g in muse["goals"] if g["id"] != "g_first"]
        resp = await client.put("/admin/personas/a_muse", json=muse)

    assert resp.status_code == 200
    path = muse_dir / "muse.yaml"
    text = path.read_text(encoding="utf-8")
    on_disk = load_yaml(path)  # 유효 yaml로 재파싱된다

    assert [g["id"] for g in on_disk["goals"]] == ["g_second"]
    assert "# 형제 항목 주석 — 교체에서 살아남아야 한다" in text
    assert "goals:                 # need = 이 목표가 걸린 욕구 축" in text
    assert text.startswith("# 합성 페르소나 —")


async def test_put_list_replacement_keeps_matched_item_comments(muse_dir):
    # 리스트 교체 — id가 살아남은 항목의 주석은 유지, 새 항목은 주석 없이 추가
    async with make_client(muse_dir) as client:
        muse = (await client.get("/admin/personas/a_muse")).json()
        muse["goals"] = [
            {"id": "g_second", "description": "둘째 목표(개정)",
             "priority": 0.6, "need": "belonging"},
            {"id": "g_third", "description": "셋째 목표",
             "priority": 0.3, "need": "security"},
        ]
        resp = await client.put("/admin/personas/a_muse", json=muse)

    assert resp.status_code == 200
    path = muse_dir / "muse.yaml"
    text = path.read_text(encoding="utf-8")
    on_disk = load_yaml(path)

    assert [g["id"] for g in on_disk["goals"]] == ["g_second", "g_third"]
    assert on_disk["goals"][0]["description"] == "둘째 목표(개정)"
    # 살아남은 항목(g_second)의 주석은 유지 — 값이 바뀌어도 id 줄 주석은 남는다
    assert "# 형제 항목 주석 — 교체에서 살아남아야 한다" in text
    # 파일 머리·형제 키 주석도 그대로
    assert text.startswith("# 합성 페르소나 —")
    assert "severity: 4          # 1(사소) ~ 5(파멸적)" in text


async def test_put_legacy_file_gains_new_keys_without_losing_comments(muse_dir):
    # active 키가 없는 구식 파일 — 저장이 키를 보태되 주석·순서를 흩뜨리지 않는다
    legacy = MUSE_YAML.replace("active: true\n", "")
    (muse_dir / "muse.yaml").write_text(legacy, encoding="utf-8")

    async with make_client(muse_dir) as client:
        muse = (await client.get("/admin/personas/a_muse")).json()
        muse["active"] = False
        resp = await client.put("/admin/personas/a_muse", json=muse)

    assert resp.status_code == 200
    path = muse_dir / "muse.yaml"
    on_disk = load_yaml(path)
    text = path.read_text(encoding="utf-8")

    assert on_disk["active"] is False
    assert text.startswith("# 합성 페르소나 —")
    assert "big_five:              # → baseline PAD 파생 (ADR-015)" in text
    assert "identity_core: |" in text


# ── 게이트 — LF_ADMIN_TOKEN 설정 시 Bearer 일치(403), dev는 열림 ─────────────


async def test_admin_token_gates_all_admin_routes(personas_dir):
    async with make_client(personas_dir, token="secret-token") as client:
        assert (await client.get("/admin/personas")).status_code == 403
        assert (
            await client.get(
                "/admin/personas", headers={"Authorization": "Bearer wrong"}
            )
        ).status_code == 403
        assert (
            await client.put("/admin/personas/a_new_face", json=valid_doc())
        ).status_code == 403
        assert (
            await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        ).status_code == 403
        ok = await client.get(
            "/admin/personas", headers={"Authorization": "Bearer secret-token"}
        )
        assert ok.status_code == 200
    assert not (personas_dir / "new-face.yaml").exists()  # 거부된 PUT은 쓰지 않는다
    assert (personas_dir / "zed.yaml").exists()  # 거부된 DELETE는 옮기지 않는다


# ── DELETE — 은퇴: yaml은 retired/로 물러나고, 소멸은 이벤트가 나른다 ─────────
#
# actor.identity.retired의 스키마 파일·발행 권한(permissions.yaml)은
# packages/schemas의 병렬 작업분이다. 등록 전까지 여기서는 적재 경로를
# 대역(fixture)으로 붙잡아 봉투 계약을 검증하고, es 실적재는 아래 통합
# 테스트가 registry 선등록으로 본다.


@pytest.fixture
def retired_events(monkeypatch) -> list:
    """es 적재 대역 — 인프라 없이 NewEvent를 붙잡는다. 실적재는 통합 테스트의 몫."""
    events: list = []

    async def capture(cfg: Config, event) -> None:
        events.append(event)

    monkeypatch.setattr(admin, "append_retired_event", capture)
    return events


@pytest.fixture
def broken_eventstore(monkeypatch) -> None:
    """es 불통 대역 — 적재 실패 시 원복(반쪽 은퇴 금지) 검증용."""

    async def explode(cfg: Config, event) -> None:
        raise RuntimeError("es가 응답하지 않는다 (대역)")

    monkeypatch.setattr(admin, "append_retired_event", explode)


async def test_delete_moves_yaml_and_appends_retired_event(personas_dir, retired_events):
    original = (personas_dir / "zed.yaml").read_text(encoding="utf-8")
    async with make_client(personas_dir) as client:
        resp = await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        listed = (await client.get("/admin/personas")).json()["personas"]
        single = await client.get("/admin/personas/a_zed")

    assert resp.status_code == 200
    assert resp.json() == {"actor_id": "a_zed", "name": "제드"}

    # ① 이동 — 삭제가 아니다: 루트에서 빠지고 retired/에 원문 그대로 남는다
    assert not (personas_dir / "zed.yaml").exists()
    assert (personas_dir / "retired" / "zed.yaml").read_text(encoding="utf-8") == original
    # roster(루트 glob)에서 이탈 — 목록·단건 모두
    assert [d["id"] for d in listed] == ["a_ari"]
    assert single.status_code == 404

    # ② 적재 봉투 — 프로젝터와 합의된 고정 계약
    [event] = retired_events
    assert event.stream == "actor" and event.stream_key == "a_zed"
    assert event.type == "actor.identity.retired"
    assert event.world_id == "w_main"  # 기본 세계
    assert event.actor_id == "a_zed" and event.tick == 0
    assert event.payload == {"actor_id": "a_zed", "name": "제드", "retired_by": "p_reaper"}


async def test_delete_unknown_persona_is_404(personas_dir, retired_events):
    async with make_client(personas_dir) as client:
        resp = await client.delete("/admin/personas/a_nobody", params={"retired_by": "p_reaper"})
    assert resp.status_code == 404
    assert retired_events == []


async def test_double_delete_is_410_and_appends_once(personas_dir, retired_events):
    async with make_client(personas_dir) as client:
        first = await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        second = await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
    assert first.status_code == 200
    assert second.status_code == 410  # 미존재(404)와 구분 — 이미 떠난 사람
    assert len(retired_events) == 1  # 은퇴는 역사에 한 번만 남는다
    assert (personas_dir / "retired" / "zed.yaml").exists()  # 보관본은 그대로


async def test_delete_requires_wellformed_retired_by(personas_dir, retired_events):
    async with make_client(personas_dir) as client:
        missing = await client.delete("/admin/personas/a_zed")
        malformed = await client.delete("/admin/personas/a_zed", params={"retired_by": "reaper"})
    assert missing.status_code == 422
    assert malformed.status_code == 422
    assert (personas_dir / "zed.yaml").exists()  # 거부된 삭제는 옮기지 않는다
    assert retired_events == []


async def test_delete_append_failure_restores_yaml(personas_dir, broken_eventstore):
    original = (personas_dir / "zed.yaml").read_text(encoding="utf-8")
    async with make_client(personas_dir) as client:
        resp = await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        listed = (await client.get("/admin/personas")).json()["personas"]

    assert resp.status_code == 500
    # 원복 — 반쪽 은퇴(파일만 물러난 상태)가 남지 않는다
    assert (personas_dir / "zed.yaml").read_text(encoding="utf-8") == original
    assert not (personas_dir / "retired" / "zed.yaml").exists()
    assert [d["id"] for d in listed] == ["a_ari", "a_zed"]  # 세계는 그대로다


async def test_delete_keeps_prior_retiree_with_same_filename(personas_dir, retired_events):
    # 같은 파일명이 이미 보관돼 있어도 역사를 덮지 않는다 — zed-2.yaml로 물러난다
    prior = ZED_YAML.replace("id: a_zed", "id: a_zed_elder").replace("name: 제드", "name: 옛 제드")
    retired = personas_dir / "retired"
    retired.mkdir()
    (retired / "zed.yaml").write_text(prior, encoding="utf-8")

    async with make_client(personas_dir) as client:
        resp = await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})

    assert resp.status_code == 200
    assert load_yaml(retired / "zed.yaml")["id"] == "a_zed_elder"  # 선임자 보존
    assert load_yaml(retired / "zed-2.yaml")["id"] == "a_zed"
    assert len(retired_events) == 1


# ── DELETE 통합 — 실제 es CAS 적재 (PG 필요, 미설정 skip — conftest 가드) ─────

#: 합의된 payload 계약의 대역 스키마 — packages/schemas 등록 전 선등록용
RETIRED_PAYLOAD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "actor_id": {"type": "string", "pattern": "^a_[a-z0-9_]+$"},
        "name": {"type": "string"},
        "retired_by": {"type": "string", "pattern": "^p_[a-z0-9_]+$"},
    },
    "required": ["actor_id", "name", "retired_by"],
    "additionalProperties": False,
}


@pytest.fixture
def preregistered_retired_schema(monkeypatch) -> None:
    """actor.identity.retired가 registry에 아직 없으면 합의 계약대로 선등록한다.

    스키마 파일과 permissions.yaml 갱신은 packages/schemas의 병렬 작업분이다 —
    이미 등록돼 있으면(정상 종착) 아무것도 덧대지 않고 실물 registry로 검증한다.
    """
    from lf_schemas import registry

    try:
        registry.payload_schema(admin.RETIRED_TYPE)
        registered = registry.is_allowed("services.gateway", admin.RETIRED_TYPE)
    except KeyError:
        registered = False
    if registered:
        return  # 실물 스키마·권한이 이미 있다 — 대역 없이 그대로 간다

    real_schema, real_allowed = registry.payload_schema, registry.is_allowed

    def payload_schema(event_type: str) -> dict:
        if event_type == admin.RETIRED_TYPE:
            return RETIRED_PAYLOAD_SCHEMA
        return real_schema(event_type)

    def is_allowed(principal: str, event_type: str) -> bool:
        if event_type == admin.RETIRED_TYPE:
            return principal == "services.gateway"
        return real_allowed(principal, event_type)

    monkeypatch.setattr(registry, "payload_schema", payload_schema)
    monkeypatch.setattr(registry, "is_allowed", is_allowed)


async def test_delete_appends_envelope_to_real_eventstore(
    personas_dir, conn, preregistered_retired_schema
):
    from lf_eventstore import current_head, read_stream
    from lf_eventstore.testing import test_database_url

    dsn = test_database_url()
    assert dsn is not None  # conn 픽스처가 스킵을 보장한다
    async with make_client(personas_dir, pg_dsn=dsn) as client:
        resp = await client.delete(
            "/admin/personas/a_zed",
            params={"retired_by": "p_reaper", "world_id": "w_test"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"actor_id": "a_zed", "name": "제드"}

    [event] = await read_stream(conn, "w_test", "actor", "a_zed")
    env = event.envelope
    assert env["type"] == "actor.identity.retired"
    assert env["stream"] == "actor"
    assert env["actor_id"] == "a_zed"
    assert env["tick"] == 0
    assert env["payload"] == {"actor_id": "a_zed", "name": "제드", "retired_by": "p_reaper"}
    assert env["correlation_id"] == env["event_id"]  # 사슬의 시작 — 자기 자신이 루트
    assert await current_head(conn, "w_test", "actor", "a_zed") == 1  # CAS head 전진


# ── 떠난 사람들 목록 + 복원 — 은퇴의 역방향 ──────────────────────────────────
#
# actor.identity.returned의 스키마 파일·발행 권한도 packages/schemas의 병렬
# 작업분이다 — 은퇴와 같은 협업 관례로, 단위 테스트는 적재 대역으로 봉투 계약을
# 보고, es 실적재는 registry 선등록(자가 판별) 통합 테스트가 본다.


@pytest.fixture
def returned_events(monkeypatch) -> list:
    """복원 es 적재 대역 — 인프라 없이 NewEvent를 붙잡는다."""
    events: list = []

    async def capture(cfg: Config, event) -> None:
        events.append(event)

    monkeypatch.setattr(admin, "append_returned_event", capture)
    return events


@pytest.fixture
def broken_returned_eventstore(monkeypatch) -> None:
    """복원 es 불통 대역 — 적재 실패 시 원복(반쪽 복원 금지) 검증용."""

    async def explode(cfg: Config, event) -> None:
        raise RuntimeError("es가 응답하지 않는다 (대역)")

    monkeypatch.setattr(admin, "append_returned_event", explode)


async def test_list_retired_is_empty_without_archive(personas_dir):
    # "retired"가 {persona_id} 경로에 잡히면 404가 난다 — 경로 순서의 회귀 감시
    async with make_client(personas_dir) as client:
        resp = await client.get("/admin/personas/retired")
    assert resp.status_code == 200
    assert resp.json() == {"retired": []}


async def test_list_retired_lists_all_archives_with_filenames(personas_dir, retired_events):
    # 같은 파일명 계보(-2 접미)의 보관본도 전부 나열한다 — 파일명이 구분자
    prior = ZED_YAML.replace("id: a_zed", "id: a_zed_elder").replace("name: 제드", "name: 옛 제드")
    retired = personas_dir / "retired"
    retired.mkdir()
    (retired / "zed.yaml").write_text(prior, encoding="utf-8")

    async with make_client(personas_dir) as client:
        await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        resp = await client.get("/admin/personas/retired")

    assert resp.status_code == 200
    # 파일명 순 — 목록(GET /admin/personas)과 같은 정렬 규약 ("zed-2" < "zed.")
    assert resp.json()["retired"] == [
        {"id": "a_zed", "name": "제드", "archetype": "night_owl",
         "filename": "zed-2.yaml"},
        {"id": "a_zed_elder", "name": "옛 제드", "archetype": "night_owl",
         "filename": "zed.yaml"},
    ]


async def test_restore_moves_yaml_back_and_appends_returned_event(
    personas_dir, retired_events, returned_events
):
    original = (personas_dir / "zed.yaml").read_text(encoding="utf-8")
    async with make_client(personas_dir) as client:
        await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        resp = await client.post(
            "/admin/personas/a_zed/restore", params={"returned_by": "p_keeper"}
        )
        listed = (await client.get("/admin/personas")).json()["personas"]
        archives = (await client.get("/admin/personas/retired")).json()["retired"]

    assert resp.status_code == 200
    assert resp.json() == {"actor_id": "a_zed", "name": "제드"}

    # ① 복귀 이동 — 원문 그대로 루트로 돌아오고, 보관함에서는 사라진다
    assert (personas_dir / "zed.yaml").read_text(encoding="utf-8") == original
    assert not (personas_dir / "retired" / "zed.yaml").exists()
    # roster(루트 glob) 합류 — 다음 tick 실행 집합에 드는 것과 같은 지문
    assert [d["id"] for d in listed] == ["a_ari", "a_zed"]
    assert archives == []

    # ② 적재 봉투 — 프로젝터와 합의된 고정 계약
    [event] = returned_events
    assert event.stream == "actor" and event.stream_key == "a_zed"
    assert event.type == "actor.identity.returned"
    assert event.world_id == "w_main"  # 기본 세계
    assert event.actor_id == "a_zed" and event.tick == 0
    assert event.payload == {"actor_id": "a_zed", "name": "제드", "returned_by": "p_keeper"}


async def test_restore_alive_persona_is_409(personas_dir, returned_events):
    # 루트에 같은 id가 살아 있으면 덮지 않는다 — 보관본이 있어도 없어도 409
    retired = personas_dir / "retired"
    retired.mkdir()
    (retired / "zed-old.yaml").write_text(ZED_YAML, encoding="utf-8")

    async with make_client(personas_dir) as client:
        resp = await client.post(
            "/admin/personas/a_zed/restore", params={"returned_by": "p_keeper"}
        )
    assert resp.status_code == 409
    assert (personas_dir / "zed.yaml").exists()  # 살아있는 사람은 그대로
    assert (retired / "zed-old.yaml").exists()  # 보관본도 그대로
    assert returned_events == []


async def test_restore_without_archive_is_404(personas_dir, returned_events):
    async with make_client(personas_dir) as client:
        resp = await client.post(
            "/admin/personas/a_nobody/restore", params={"returned_by": "p_keeper"}
        )
    assert resp.status_code == 404
    assert returned_events == []


async def test_restore_requires_wellformed_returned_by(personas_dir, returned_events):
    retired = personas_dir / "retired"
    retired.mkdir()
    ghost = ZED_YAML.replace("id: a_zed", "id: a_ghost")
    (retired / "ghost.yaml").write_text(ghost, encoding="utf-8")

    async with make_client(personas_dir) as client:
        missing = await client.post("/admin/personas/a_ghost/restore")
        malformed = await client.post(
            "/admin/personas/a_ghost/restore", params={"returned_by": "keeper"}
        )
    assert missing.status_code == 422
    assert malformed.status_code == 422
    assert (retired / "ghost.yaml").exists()  # 거부된 복원은 옮기지 않는다
    assert returned_events == []


async def test_restore_append_failure_returns_yaml_to_archive(
    personas_dir, broken_returned_eventstore
):
    retired = personas_dir / "retired"
    retired.mkdir()
    ghost = ZED_YAML.replace("id: a_zed", "id: a_ghost").replace("name: 제드", "name: 유령")
    (retired / "ghost.yaml").write_text(ghost, encoding="utf-8")

    async with make_client(personas_dir) as client:
        resp = await client.post(
            "/admin/personas/a_ghost/restore", params={"returned_by": "p_keeper"}
        )
        listed = (await client.get("/admin/personas")).json()["personas"]

    assert resp.status_code == 500
    # 원복 — 반쪽 복원(파일만 돌아온 상태)이 남지 않는다
    assert (retired / "ghost.yaml").read_text(encoding="utf-8") == ghost
    assert not (personas_dir / "ghost.yaml").exists()
    assert [d["id"] for d in listed] == ["a_ari", "a_zed"]  # 세계는 그대로다


async def test_restore_sidesteps_root_filename_collision(personas_dir, returned_events):
    # 보관본과 같은 파일명을 루트의 다른 id가 쓰고 있어도 덮지 않는다 — zed-2.yaml로 비껴 앉는다
    retired = personas_dir / "retired"
    retired.mkdir()
    elder = ZED_YAML.replace("id: a_zed", "id: a_zed_elder").replace("name: 제드", "name: 옛 제드")
    (retired / "zed.yaml").write_text(elder, encoding="utf-8")

    async with make_client(personas_dir) as client:
        resp = await client.post(
            "/admin/personas/a_zed_elder/restore", params={"returned_by": "p_keeper"}
        )
    assert resp.status_code == 200
    assert load_yaml(personas_dir / "zed.yaml")["id"] == "a_zed"  # 살아있는 파일 보존
    assert load_yaml(personas_dir / "zed-2.yaml")["id"] == "a_zed_elder"
    assert len(returned_events) == 1


async def test_admin_token_gates_restore_routes(personas_dir, returned_events):
    async with make_client(personas_dir, token="secret-token") as client:
        assert (await client.get("/admin/personas/retired")).status_code == 403
        assert (
            await client.post(
                "/admin/personas/a_zed/restore", params={"returned_by": "p_keeper"}
            )
        ).status_code == 403
        ok = await client.get(
            "/admin/personas/retired", headers={"Authorization": "Bearer secret-token"}
        )
        assert ok.status_code == 200
    assert returned_events == []


# ── DELETE /retired/{filename} — 영구 삭제: 보관본만 지운다(역사 이벤트 불변) ──


async def test_purge_removes_archive_and_blocks_restore(personas_dir, retired_events):
    async with make_client(personas_dir) as client:
        await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})
        resp = await client.delete("/admin/personas/retired/zed.yaml")
        archives = (await client.get("/admin/personas/retired")).json()["retired"]
        restore = await client.post(
            "/admin/personas/a_zed/restore", params={"returned_by": "p_keeper"}
        )

    assert resp.status_code == 200
    assert resp.json() == {"filename": "zed.yaml", "name": "제드"}
    # 보관본이 사라졌다 — 목록에서도, 복원 경로에서도 (되돌릴 수 없다)
    assert not (personas_dir / "retired" / "zed.yaml").exists()
    assert archives == []
    assert restore.status_code == 404
    # 은퇴 이벤트(역사)는 그대로 — 영구 삭제는 파일만 지운다 (새 이벤트 없음)
    assert len(retired_events) == 1


async def test_purge_unknown_filename_is_404(personas_dir):
    async with make_client(personas_dir) as client:
        resp = await client.delete("/admin/personas/retired/nobody.yaml")
    assert resp.status_code == 404


async def test_purge_targets_only_the_archive_never_root(personas_dir, retired_events):
    # /retired/{filename}은 보관함만 겨눈다 — 루트의 살아있는 페르소나는 못 지운다
    async with make_client(personas_dir) as client:
        resp = await client.delete("/admin/personas/retired/ari.yaml")
        listed = (await client.get("/admin/personas")).json()["personas"]

    assert resp.status_code == 404  # 보관함에 없다 (루트 파일이라도)
    assert (personas_dir / "ari.yaml").exists()  # 살아있는 사람은 안전하다
    assert [d["id"] for d in listed] == ["a_ari", "a_zed"]
    assert retired_events == []


async def test_purge_removes_only_the_named_archive(personas_dir, retired_events):
    # 같은 id의 보관본이 여럿(-2)이어도 filename으로 하나만 지운다
    prior = ZED_YAML.replace("id: a_zed", "id: a_zed_elder").replace("name: 제드", "name: 옛 제드")
    retired = personas_dir / "retired"
    retired.mkdir()
    (retired / "zed.yaml").write_text(prior, encoding="utf-8")

    async with make_client(personas_dir) as client:
        await client.delete("/admin/personas/a_zed", params={"retired_by": "p_reaper"})  # zed-2
        resp = await client.delete("/admin/personas/retired/zed-2.yaml")
        remaining = (await client.get("/admin/personas/retired")).json()["retired"]

    assert resp.status_code == 200
    assert not (retired / "zed-2.yaml").exists()
    assert [r["filename"] for r in remaining] == ["zed.yaml"]  # 선임자 보존


async def test_admin_token_gates_purge(personas_dir, retired_events):
    async with make_client(personas_dir, token="secret-token") as client:
        await client.delete(
            "/admin/personas/a_zed",
            params={"retired_by": "p_reaper"},
            headers={"Authorization": "Bearer secret-token"},
        )
        denied = await client.delete("/admin/personas/retired/zed.yaml")
    assert denied.status_code == 403
    assert (personas_dir / "retired" / "zed.yaml").exists()  # 거부된 삭제는 지우지 않는다


# ── 복원 통합 — 실제 es CAS 적재 (PG 필요, 미설정 skip — conftest 가드) ───────

#: 합의된 payload 계약의 대역 스키마 — packages/schemas 등록 전 선등록용
RETURNED_PAYLOAD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "actor_id": {"type": "string", "pattern": "^a_[a-z0-9_]+$"},
        "name": {"type": "string"},
        "returned_by": {"type": "string", "pattern": "^p_[a-z0-9_]+$"},
    },
    "required": ["actor_id", "name", "returned_by"],
    "additionalProperties": False,
}


@pytest.fixture
def preregistered_returned_schema(monkeypatch) -> None:
    """actor.identity.returned가 registry에 아직 없으면 합의 계약대로 선등록한다.

    스키마 파일과 permissions.yaml 갱신은 packages/schemas의 병렬 작업분이다 —
    이미 등록돼 있으면(정상 종착) 아무것도 덧대지 않고 실물 registry로 검증한다.
    """
    from lf_schemas import registry

    try:
        registry.payload_schema(admin.RETURNED_TYPE)
        registered = registry.is_allowed("services.gateway", admin.RETURNED_TYPE)
    except KeyError:
        registered = False
    if registered:
        return  # 실물 스키마·권한이 이미 있다 — 대역 없이 그대로 간다

    real_schema, real_allowed = registry.payload_schema, registry.is_allowed

    def payload_schema(event_type: str) -> dict:
        if event_type == admin.RETURNED_TYPE:
            return RETURNED_PAYLOAD_SCHEMA
        return real_schema(event_type)

    def is_allowed(principal: str, event_type: str) -> bool:
        if event_type == admin.RETURNED_TYPE:
            return principal == "services.gateway"
        return real_allowed(principal, event_type)

    monkeypatch.setattr(registry, "payload_schema", payload_schema)
    monkeypatch.setattr(registry, "is_allowed", is_allowed)


async def test_restore_appends_envelope_to_real_eventstore(
    personas_dir, conn, preregistered_retired_schema, preregistered_returned_schema
):
    # 은퇴 → 복원 전체 왕복 — 한 stream에 두 사건이 차례로 남고 head가 전진한다
    from lf_eventstore import current_head, read_stream
    from lf_eventstore.testing import test_database_url

    dsn = test_database_url()
    assert dsn is not None  # conn 픽스처가 스킵을 보장한다
    async with make_client(personas_dir, pg_dsn=dsn) as client:
        gone = await client.delete(
            "/admin/personas/a_zed",
            params={"retired_by": "p_reaper", "world_id": "w_test"},
        )
        back = await client.post(
            "/admin/personas/a_zed/restore",
            params={"returned_by": "p_keeper", "world_id": "w_test"},
        )
    assert gone.status_code == 200
    assert back.status_code == 200
    assert back.json() == {"actor_id": "a_zed", "name": "제드"}
    assert (personas_dir / "zed.yaml").exists()  # 세계의 실행 집합으로 복귀

    events = await read_stream(conn, "w_test", "actor", "a_zed")
    retired_env, returned_env = [e.envelope for e in events]
    assert retired_env["type"] == "actor.identity.retired"
    assert returned_env["type"] == "actor.identity.returned"
    assert returned_env["stream"] == "actor"
    assert returned_env["actor_id"] == "a_zed"
    assert returned_env["tick"] == 0
    assert returned_env["payload"] == {
        "actor_id": "a_zed", "name": "제드", "returned_by": "p_keeper",
    }
    assert returned_env["correlation_id"] == returned_env["event_id"]  # 스튜디오 개입은 새 사슬
    assert await current_head(conn, "w_test", "actor", "a_zed") == 2  # CAS head 전진
