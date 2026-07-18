"""페르소나 스튜디오 관리 API — agents/personas 파일(SoT)의 CRUD 중재.

사람이 AI 페르소나를 빚고 세계에 풀어놓는 창조자 도구의 백엔드다 (ADR-001/012
— 파일이 원천, DB화하지 않는다). 이 API는 es에 아무것도 적재하지 않는다:
세계 반영은 tick 워커의 핫 리로드(lf_actor.reload) 몫이다.

계약(FE 병렬 개발 중 — 고정):
  GET  /admin/personas       → {"personas": [PersonaDoc...]} (파일명 순)
  GET  /admin/personas/{id}  → PersonaDoc (없으면 404 {"detail": ...})
  PUT  /admin/personas/{id}  → 검증(422) 후 yaml 저장, 저장된 PersonaDoc 반환.
                               created_by는 생성 시에만 — 수정으로 못 바꾼다.
게이트: LF_ADMIN_TOKEN 설정 시 Authorization: Bearer 일치(403), dev는 열림.
"""

from __future__ import annotations

import hmac
import io
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from lf_gateway.config import Config

BIG_FIVE_KEYS = frozenset(
    {"openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"}
)
NEEDS_KEYS = frozenset({"achievement", "belonging", "security"})

#: 저장 시 스튜디오가 소유하는 최상위 키 — 이 밖의 키는 파일의 결로 보존한다
_OWNED_KEYS = frozenset(
    {"id", "name", "archetype", "lifestyle", "active", "created_by",
     "big_five", "identity_core", "needs_bias", "goals", "secrets"}
)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


class GoalDoc(BaseModel):
    id: str = Field(pattern=r"^g_[a-z0-9_]+$")
    description: str = Field(min_length=1)
    priority: float
    need: Literal["achievement", "belonging", "security"]

    @field_validator("priority")
    @classmethod
    def _clamp_priority(cls, v: float) -> float:
        return _clamp01(v)


class SecretDoc(BaseModel):
    id: str = Field(pattern=r"^s_[a-z0-9_]+$")
    description: str = Field(min_length=1)


class PersonaDoc(BaseModel):
    """관리 API의 페르소나 표면 — 파일의 결(severity 등 내부 키)은 노출하지 않는다."""

    id: str = Field(pattern=r"^a_[a-z0-9_]+$")
    name: str
    archetype: str = ""
    lifestyle: Literal["office_worker", "student", "teacher", "night_worker", "flexible"]
    active: bool = True
    created_by: str | None = Field(default=None, pattern=r"^p_[a-z0-9_]+$")
    big_five: dict[str, float]
    needs_bias: dict[str, float]
    goals: list[GoalDoc] = Field(default_factory=list)
    secrets: list[SecretDoc] = Field(default_factory=list)
    identity_core: str = ""

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("이름이 비어 있다")
        return v.strip()

    @field_validator("identity_core")
    @classmethod
    def _strip_core(cls, v: str) -> str:
        return v.strip()

    @field_validator("big_five")
    @classmethod
    def _big_five_complete(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != BIG_FIVE_KEYS:
            raise ValueError(f"big_five 키가 불완전하다 — 필요: {sorted(BIG_FIVE_KEYS)}")
        return {k: _clamp01(v[k]) for k in v}

    @field_validator("needs_bias")
    @classmethod
    def _needs_complete(cls, v: dict[str, float]) -> dict[str, float]:
        if set(v) != NEEDS_KEYS:
            raise ValueError(f"needs_bias 키가 불완전하다 — 필요: {sorted(NEEDS_KEYS)}")
        return {k: _clamp01(v[k]) for k in v}


# ── 파일 계층 — 파일명 순이 목록 순서, id 스캔이 위치 규칙 ───────────────────


def persona_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.yaml"))


def read_doc(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def find_persona_file(directory: Path, persona_id: str) -> Path | None:
    """기존 파일은 id 스캔으로 위치한다 — 파일명은 id와 독립이다 (기존 결 존중)."""
    for path in persona_files(directory):
        if read_doc(path).get("id") == persona_id:
            return path
    return None


def new_persona_path(directory: Path, persona_id: str) -> Path:
    """신규 파일명 — id에서 a_ 접두 제거, 언더스코어→하이픈 (기존 파일명 결)."""
    stem = persona_id.removeprefix("a_").replace("_", "-")
    return directory / f"{stem}.yaml"


def leading_comment(text: str) -> str:
    """파일 머리의 연속 주석 줄들 — 저장 시 보존한다 (기존 파일 결)."""
    lines = []
    for line in text.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "".join(f"{line}\n" for line in lines)


def _merge_items(
    new_items: list[dict[str, Any]], old_items: Any
) -> list[dict[str, Any]]:
    """id가 같은 항목의 미지 키(severity 등)를 보존한다 — 스튜디오는 모르는 결을 지우지 않는다."""
    old_by_id = {
        item["id"]: item
        for item in (old_items or [])
        if isinstance(item, dict) and "id" in item
    }
    return [{**old_by_id.get(item["id"], {}), **item} for item in new_items]


def merged_yaml_doc(doc: PersonaDoc, old: dict[str, Any] | None) -> dict[str, Any]:
    """저장할 yaml 문서 — 캐논 키 순서 + 저자성 불변 + 미지 키 보존.

    created_by는 생성(old=None) 시에만 본문 값을 받는다. 수정 시엔 기존 파일
    값이 권위다 — 저자성은 수정으로 지워지지도, 바뀌지도 않는다.
    """
    data = doc.model_dump()
    created_by = data["created_by"] if old is None else old.get("created_by")
    out: dict[str, Any] = {
        "id": data["id"],
        "name": data["name"],
        "archetype": data["archetype"],
        "lifestyle": data["lifestyle"],
        "active": data["active"],
    }
    if created_by is not None:
        out["created_by"] = created_by
    out["big_five"] = data["big_five"]
    out["identity_core"] = data["identity_core"]
    out["needs_bias"] = data["needs_bias"]
    out["goals"] = _merge_items(data["goals"], (old or {}).get("goals"))
    out["secrets"] = _merge_items(data["secrets"], (old or {}).get("secrets"))
    for key, value in (old or {}).items():
        if key not in _OWNED_KEYS:  # 미래 필드·수동 편집의 결 보존
            out[key] = value
    return out


class _StudioDumper(yaml.SafeDumper):
    """여러 줄 문자열(identity_core)은 블록 리터럴로 — 기존 파일 결."""


def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_StudioDumper.add_representer(str, _str_representer)


def dump_persona_yaml(doc: dict[str, Any], header: str) -> str:
    body = yaml.dump(
        doc, Dumper=_StudioDumper, allow_unicode=True, sort_keys=False, width=1000
    )
    return header + body


def write_persona_file(path: Path, text: str) -> None:
    # UTF-8, BOM 없이, LF — 기존 파일 결 (Windows에서도 CRLF로 번역하지 않는다)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ── 왕복 편집 — 기존 파일의 수기 주석·키 순서·블록 스칼라는 설계 기록이다 ─────
#
# yaml이 SoT(ADR-001/012)라 파일 속 주석이 곧 문서다. 저장이 재직렬화로
# 그걸 지우면 안 되므로, 기존 파일은 ruamel round-trip으로 로드해
# 변경 필드만 제자리 갱신한다. 한계: 교체·삭제된 리스트 항목에 붙어 있던
# 주석은 항목과 함께 사라진다 — 파일 머리·형제 키 주석은 남는다.


def _round_trip_yaml() -> YAML:
    rt = YAML()  # round-trip 모드가 기본 — 주석·키 순서·스타일 보존
    rt.preserve_quotes = True
    rt.allow_duplicate_keys = True  # 수기 편집 실수로 저장이 죽지 않게 (뒤 값이 이긴다)
    rt.width = 1000  # 현행 덤프와 동일 — 긴 문장을 접지 않는다
    rt.indent(mapping=2, sequence=4, offset=2)  # `  - id: ...` — 기존 파일 결
    return rt


def _literal_block(value: str) -> LiteralScalarString:
    """여러 줄 문자열의 블록 리터럴(|) 표기 — 기존 파일 결."""
    return LiteralScalarString(value if value.endswith("\n") else value + "\n")


def _fresh_node(value: Any) -> Any:
    if isinstance(value, dict):
        return CommentedMap((k, _fresh_node(v)) for k, v in value.items())
    if isinstance(value, list):
        return CommentedSeq(_fresh_node(v) for v in value)
    if isinstance(value, str) and "\n" in value:
        return _literal_block(value)
    return value


def _scalar_equal(old: Any, new: Any) -> bool:
    if isinstance(old, str) and isinstance(new, str):
        # 블록 스칼라의 꼬리 개행 등 표기 차이는 같음으로 친다 — 안 바뀐 값은 안 건드린다
        return old.strip() == new.strip()
    return bool(old == new)


def _apply_sequence(target: CommentedSeq, desired: list[Any]) -> None:
    """id가 같은 항목은 노드를 재사용한다 — 항목 안 주석(severity 등)이 산다."""
    old_by_id = {
        item["id"]: item
        for item in target
        if isinstance(item, CommentedMap) and "id" in item
    }
    rebuilt: list[Any] = []
    for value in desired:
        node = old_by_id.get(value.get("id")) if isinstance(value, dict) else None
        if node is not None:
            _apply_mapping(node, value)
            rebuilt.append(node)
        else:
            rebuilt.append(_fresh_node(value))
    while len(target):
        target.pop()
    target.extend(rebuilt)


def _apply_mapping(target: CommentedMap, desired: dict[str, Any]) -> None:
    """desired를 target에 제자리 반영 — 있던 키는 위치·주석 유지, 새 키는 이웃하게 삽입."""
    for key in [k for k in target if k not in desired]:
        del target[key]
    prev_index = -1
    for key, value in desired.items():
        if key not in target:
            target.insert(prev_index + 1, key, _fresh_node(value))
        else:
            old = target[key]
            if isinstance(value, dict) and isinstance(old, CommentedMap):
                _apply_mapping(old, value)
            elif isinstance(value, list) and isinstance(old, CommentedSeq) and value and len(old):
                _apply_sequence(old, value)
            elif isinstance(value, dict | list) or isinstance(old, dict | list):
                target[key] = _fresh_node(value)  # 형태가 바뀌면 새로 빚는다 (빈 리스트 포함)
            elif not _scalar_equal(old, value):
                literal = "\n" in value if isinstance(value, str) else False
                if literal or isinstance(old, LiteralScalarString):
                    target[key] = _literal_block(value)  # 블록 리터럴 결 유지
                else:
                    target[key] = value
        prev_index = list(target).index(key)


def update_persona_text(text: str, merged: dict[str, Any]) -> str:
    """기존 yaml 텍스트에 merged 문서를 왕복 편집으로 반영한 새 텍스트."""
    rt = _round_trip_yaml()
    root = rt.load(text)
    if not isinstance(root, CommentedMap):  # 비정형 파일 — 머리 주석만 살려 전체 재작성
        return dump_persona_yaml(merged, leading_comment(text))
    _apply_mapping(root, merged)
    buf = io.StringIO()
    rt.dump(root, buf)
    return buf.getvalue()


# ── 라우터 ───────────────────────────────────────────────────────────────────


def create_admin_router(cfg: Config) -> APIRouter:
    async def require_admin(authorization: Annotated[str, Header()] = "") -> None:
        if cfg.admin_token is None:
            return  # dev 개방 — 로컬 밖 노출 전 LF_ADMIN_TOKEN을 설정하라
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credential.strip(), cfg.admin_token
        ):
            raise HTTPException(403, "관리 토큰이 필요하다 (LF_ADMIN_TOKEN)")

    router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

    @router.get("/personas")
    async def list_personas() -> dict[str, list[PersonaDoc]]:
        docs = [PersonaDoc.model_validate(read_doc(p)) for p in persona_files(cfg.personas_dir)]
        return {"personas": docs}

    @router.get("/personas/{persona_id}")
    async def get_persona(persona_id: str) -> PersonaDoc:
        path = find_persona_file(cfg.personas_dir, persona_id)
        if path is None:
            raise HTTPException(404, f"페르소나가 없다: {persona_id}")
        return PersonaDoc.model_validate(read_doc(path))

    @router.put("/personas/{persona_id}")
    async def put_persona(persona_id: str, doc: PersonaDoc) -> PersonaDoc:
        if doc.id != persona_id:
            raise HTTPException(422, f"본문 id({doc.id})와 경로 id({persona_id})가 다르다")
        path = find_persona_file(cfg.personas_dir, persona_id)
        if path is None:
            path = new_persona_path(cfg.personas_dir, persona_id)
            if path.exists():  # 다른 id가 이미 쓰는 파일명 — 덮어쓰지 않는다
                raise HTTPException(409, f"파일명이 이미 쓰인다: {path.name}")
            merged = merged_yaml_doc(doc, None)
            content = dump_persona_yaml(merged, f"# {doc.name} — Persona Studio\n")
        else:
            text = path.read_text(encoding="utf-8")
            merged = merged_yaml_doc(doc, yaml.safe_load(text))
            content = update_persona_text(text, merged)  # 수기 주석·키 순서 보존
        write_persona_file(path, content)
        return PersonaDoc.model_validate(merged)  # 저장본 — created_by 불변이 반영된 값

    return router
