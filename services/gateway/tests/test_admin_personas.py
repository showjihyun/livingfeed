"""페르소나 스튜디오 관리 API — 파일(agents/personas, SoT)의 CRUD 중재.

es에 아무것도 적재하지 않는다 — 세계 반영은 tick 워커 핫 리로드의 몫.
전부 tmp_path 페르소나 디렉터리 — 인프라(NATS/PG/Redis) 없이 돈다
(lifespan을 태우지 않는 ASGITransport).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
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


def make_client(personas_dir: Path, *, token: str | None = None) -> httpx.AsyncClient:
    cfg = Config(
        nats_url="nats://localhost:4222", env="test",
        personas_dir=personas_dir, admin_token=token,
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
        ok = await client.get(
            "/admin/personas", headers={"Authorization": "Bearer secret-token"}
        )
        assert ok.status_code == 200
    assert not (personas_dir / "new-face.yaml").exists()  # 거부된 PUT은 쓰지 않는다
