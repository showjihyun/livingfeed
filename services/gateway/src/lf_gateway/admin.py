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
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

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
            old = None
            path = new_persona_path(cfg.personas_dir, persona_id)
            if path.exists():  # 다른 id가 이미 쓰는 파일명 — 덮어쓰지 않는다
                raise HTTPException(409, f"파일명이 이미 쓰인다: {path.name}")
            header = f"# {doc.name} — Persona Studio\n"
        else:
            text = path.read_text(encoding="utf-8")
            old = yaml.safe_load(text)
            header = leading_comment(text)
        merged = merged_yaml_doc(doc, old)
        write_persona_file(path, dump_persona_yaml(merged, header))
        return PersonaDoc.model_validate(merged)  # 저장본 — created_by 불변이 반영된 값

    return router
