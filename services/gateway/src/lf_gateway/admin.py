"""페르소나 스튜디오 관리 API — agents/personas 파일(SoT)의 CRUD 중재.

사람이 AI 페르소나를 빚고 세계에 풀어놓는 창조자 도구의 백엔드다 (ADR-001/012
— 파일이 원천, DB화하지 않는다). 조회·저장은 es에 아무것도 적재하지 않는다:
세계 반영은 tick 워커의 핫 리로드(lf_actor.reload) 몫이다. 예외는 은퇴(DELETE)와
복원(restore) 둘이다 — read 모델의 소멸·귀환은 프로젝터가 집행해야 하므로
actor.identity.retired / actor.identity.returned를 es에 적재한다(파일 이동은
roster 이탈·합류만 담당한다).

계약(FE 병렬 개발 중 — 고정):
  GET    /admin/personas       → {"personas": [PersonaDoc...]} (파일명 순)
  GET    /admin/personas/retired
         → {"retired": [{id, name, archetype, filename}...]} (파일명 순).
           같은 id의 보관본이 여럿(-2, -3…)이어도 전부 나열한다 — 파일명이 구분자.
  GET    /admin/personas/{id}  → PersonaDoc (없으면 404 {"detail": ...})
  PUT    /admin/personas/{id}  → 검증(422) 후 yaml 저장, 저장된 PersonaDoc 반환.
                                 created_by는 생성 시에만 — 수정으로 못 바꾼다.
  DELETE /admin/personas/{id}?retired_by=p_* [&world_id=w_main]
         → ① yaml을 retired/ 하위로 이동(삭제 아님 — 역사 보존, 로더는 루트만
           보므로 roster에서 빠진다) ② actor.identity.retired 적재(es CAS).
           200 {actor_id, name} / 404(미존재) / 410(이미 은퇴).
           이동 성공·적재 실패는 yaml 원복 후 500 — 반쪽 은퇴를 남기지 않는다.
  POST   /admin/personas/{id}/restore?returned_by=p_* [&world_id=w_main]
         → 은퇴의 역방향. ① retired/의 yaml을 루트로 복귀 이동(로더가 루트만
           보므로 다음 tick 실행 집합에 합류) ② actor.identity.returned 적재.
           200 {actor_id, name} / 409(루트에 동일 id 생존 — 덮지 않는다) /
           404(보관본 없음). 이동 성공·적재 실패는 yaml을 retired/로 원복 후
           500 — 반쪽 복원을 남기지 않는다.
게이트: LF_ADMIN_TOKEN 설정 시 Authorization: Bearer 일치(403), dev는 열림.
"""

from __future__ import annotations

import hmac
import io
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from lf_eventstore import ConcurrencyConflict, NewEvent, append, current_head
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString

from lf_gateway.config import Config
from lf_gateway.session import PRINCIPAL

logger = logging.getLogger("lf.gateway.admin")

BIG_FIVE_KEYS = frozenset(
    {"openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"}
)
NEEDS_KEYS = frozenset({"achievement", "belonging", "security"})

#: 저장 시 스튜디오가 소유하는 최상위 키 — 이 밖의 키는 파일의 결로 보존한다
_OWNED_KEYS = frozenset(
    {"id", "name", "archetype", "lifestyle", "active", "created_by", "community",
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
    #: 커뮤니티 소속(^c_) — 커뮤니티 피드의 소속 원천 (ADR-014). 무소속은 None.
    community: str | None = Field(default=None, pattern=r"^c_[a-z0-9_]+$")
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
    if data.get("community") is not None:
        out["community"] = data["community"]  # 무소속은 키 자체를 쓰지 않는다
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


# ── 은퇴·복원 — 파일은 retired/를 오가고(roster 이탈·합류), 소멸·귀환은 이벤트가 나른다 ────
#
# 파일 이동은 세계의 실행 집합에서 빼고 넣는 것까지만이다 (reload 지문은 루트 glob).
# 이미 세계에 남긴 글·관계(read 모델)의 소멸(retired)과 귀환(returned)은
# 프로젝터가 이벤트를 소비해 집행한다 — 그래서 이 두 경로만 예외적으로 es에 적재한다.

#: 은퇴한 페르소나 yaml의 보관처 — 삭제하지 않는다 (역사 보존의 결)
RETIRED_DIRNAME = "retired"
#: 은퇴 이벤트 계약 (프로젝터와 합의된 고정 계약) — stream=actor, stream_key=actor_id
RETIRED_TYPE = "actor.identity.retired"
#: 복원 이벤트 계약 (프로젝터와 합의된 고정 계약) — 은퇴의 역방향, 같은 stream/key
RETURNED_TYPE = "actor.identity.returned"


def vacant_path(directory: Path, filename: str) -> Path:
    """directory 하위의 비어 있는 목적지 — 같은 이름이 있으면 -2, -3… (있는 것을 덮지 않는다)."""
    dest = directory / filename
    stem, suffix = dest.stem, dest.suffix
    counter = 2
    while dest.exists():
        dest = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return dest


def retire_destination(directory: Path, filename: str) -> Path:
    """retired/ 하위의 비어 있는 목적지 — 역사를 덮지 않는다."""
    return vacant_path(directory / RETIRED_DIRNAME, filename)


def build_retired_event(
    world_id: str, actor_id: str, name: str, retired_by: str
) -> NewEvent:
    """actor.identity.retired NewEvent — payload는 프로젝터와의 고정 계약."""
    return NewEvent(
        world_id=world_id,
        stream="actor",
        stream_key=actor_id,
        type=RETIRED_TYPE,
        # 스튜디오 개입은 tick 밖의 사건이다 — player.* 와 같은 tick 0 규약 (session.py)
        tick=0,
        actor_id=actor_id,
        payload={"actor_id": actor_id, "name": name, "retired_by": retired_by},
    )


def build_returned_event(
    world_id: str, actor_id: str, name: str, returned_by: str
) -> NewEvent:
    """actor.identity.returned NewEvent — payload는 프로젝터와의 고정 계약."""
    return NewEvent(
        world_id=world_id,
        stream="actor",
        stream_key=actor_id,
        type=RETURNED_TYPE,
        tick=0,  # 스튜디오 개입은 tick 밖의 사건 — 은퇴와 같은 규약
        actor_id=actor_id,
        payload={"actor_id": actor_id, "name": name, "returned_by": returned_by},
    )


async def _append_with_cas(cfg: Config, event: NewEvent) -> None:
    """es CAS 적재 — 경합이면 재수화 후 1회 재시도 (session.py 커맨드 적재 관례)."""
    conn = await AsyncConnection.connect(cfg.pg_dsn, autocommit=True)
    try:
        head = await current_head(conn, event.world_id, event.stream, event.stream_key)
        try:
            await append(conn, PRINCIPAL, [event], expected_head=head)
        except ConcurrencyConflict:
            head = await current_head(conn, event.world_id, event.stream, event.stream_key)
            await append(conn, PRINCIPAL, [event], expected_head=head)
    finally:
        await conn.close()


# 적재 함수는 경로별로 나뉜다 — 테스트가 은퇴/복원의 대역(fixture)을 따로 붙잡는 이음새
async def append_retired_event(cfg: Config, event: NewEvent) -> None:
    """은퇴 이벤트의 es CAS 적재."""
    await _append_with_cas(cfg, event)


async def append_returned_event(cfg: Config, event: NewEvent) -> None:
    """복원 이벤트의 es CAS 적재."""
    await _append_with_cas(cfg, event)


class RetiredSummary(BaseModel):
    """떠난 사람의 명단 한 줄 — 전체 문서가 아니라 알아볼 표식만.

    filename이 구분자다: 같은 id가 여러 번 은퇴하면 보관본이 -2, -3…으로
    쌓이는데, 목록은 전부 나열하고 파일명으로 구분해 보여준다.
    """

    id: str
    name: str
    archetype: str = ""
    filename: str


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

    # 경로 규칙: /personas/{persona_id}보다 먼저 서야 "retired"가 id로 잡히지 않는다
    @router.get("/personas/retired")
    async def list_retired() -> dict[str, list[RetiredSummary]]:
        resting = cfg.personas_dir / RETIRED_DIRNAME
        if not resting.is_dir():
            return {"retired": []}
        summaries: list[RetiredSummary] = []
        for path in persona_files(resting):
            try:
                doc = read_doc(path)
                actor_id = doc.get("id") if isinstance(doc, dict) else None
                if not actor_id:
                    raise ValueError("id가 없다")
            except Exception as e:
                # 손상된 보관본이 목록 전체를 죽이면 안 된다 — 건너뛰되 흔적은 남긴다
                # (id 없는 파일은 복원 경로(id 스캔)로도 못 찾으니 목록에 세울 수 없다)
                logger.warning("보관본을 읽지 못해 목록에서 건너뛴다: %s (%s)", path.name, e)
                continue
            summaries.append(
                RetiredSummary(
                    id=str(actor_id),
                    name=str(doc.get("name") or actor_id),
                    archetype=str(doc.get("archetype") or ""),
                    filename=path.name,
                )
            )
        return {"retired": summaries}

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

    @router.delete("/personas/{persona_id}")
    async def delete_persona(
        persona_id: str,
        retired_by: str = Query(..., pattern=r"^p_[a-z0-9_]+$"),
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
    ) -> dict[str, str]:
        path = find_persona_file(cfg.personas_dir, persona_id)
        if path is None:
            resting = cfg.personas_dir / RETIRED_DIRNAME
            if resting.is_dir() and find_persona_file(resting, persona_id) is not None:
                raise HTTPException(410, f"이미 세계를 떠난 사람이다: {persona_id}")
            raise HTTPException(404, f"페르소나가 없다: {persona_id}")
        name = str(read_doc(path).get("name") or persona_id)

        # ① 이동 — 삭제가 아니다(역사 보존). 실패면 여기서 중단, 세계는 그대로다
        dest = retire_destination(cfg.personas_dir, path.name)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            path.replace(dest)
        except OSError as e:
            raise HTTPException(500, f"페르소나 파일을 옮기지 못했다: {e}") from e

        # ② 적재 — 실패면 yaml 원복 후 5xx. 반쪽 은퇴(파일만 물러난 상태)를 남기지 않는다
        try:
            await append_retired_event(
                cfg, build_retired_event(world_id, persona_id, name, retired_by)
            )
        except Exception as e:
            try:
                dest.replace(path)
            except OSError:
                logger.exception(
                    "은퇴 원복 실패 — 수동 복구 필요: %s/%s", RETIRED_DIRNAME, dest.name
                )
                raise HTTPException(
                    500,
                    f"적재와 원복이 모두 실패했다 — {RETIRED_DIRNAME}/{dest.name}를 수동 복구하라",
                ) from e
            raise HTTPException(500, f"세계 역사 적재에 실패했다 (은퇴는 취소됨): {e}") from e
        return {"actor_id": persona_id, "name": name}

    @router.post("/personas/{persona_id}/restore")
    async def restore_persona(
        persona_id: str,
        returned_by: str = Query(..., pattern=r"^p_[a-z0-9_]+$"),
        world_id: str = Query("w_main", pattern=r"^w_[a-z0-9_]+$"),
    ) -> dict[str, str]:
        # 살아있는 사람 우선 — 루트에 같은 id가 있으면 덮지도, 겹치지도 않는다.
        # (복원 직후의 이중 클릭도 여기로 온다 — 404가 아니라 "이미 살아 있다")
        if find_persona_file(cfg.personas_dir, persona_id) is not None:
            raise HTTPException(409, f"이미 세계에 살아 있는 사람이다: {persona_id}")
        resting = cfg.personas_dir / RETIRED_DIRNAME
        archived = (
            find_persona_file(resting, persona_id) if resting.is_dir() else None
        )
        if archived is None:
            raise HTTPException(404, f"보관된 페르소나가 없다: {persona_id}")
        name = str(read_doc(archived).get("name") or persona_id)

        # ① 복귀 이동 — 로더는 루트만 보므로 이 이동만으로 다음 tick 실행 집합에
        # 합류한다. 파일명은 보관본 그대로 옮기되, 루트에 같은 이름의 다른 파일이
        # 있으면 -2, -3…으로 비껴 앉는다 (id 충돌은 위 409가 이미 막았다)
        dest = vacant_path(cfg.personas_dir, archived.name)
        try:
            archived.replace(dest)
        except OSError as e:
            raise HTTPException(500, f"페르소나 파일을 되돌리지 못했다: {e}") from e

        # ② 적재 — 실패면 yaml을 retired/로 원복 후 5xx. 반쪽 복원(파일만 돌아온
        # 상태)을 남기지 않는다 — 은퇴와 대칭인 규약
        try:
            await append_returned_event(
                cfg, build_returned_event(world_id, persona_id, name, returned_by)
            )
        except Exception as e:
            try:
                dest.replace(archived)
            except OSError:
                logger.exception(
                    "복원 원복 실패 — 수동 복구 필요: %s", dest.name
                )
                raise HTTPException(
                    500,
                    f"적재와 원복이 모두 실패했다 — 루트의 {dest.name}를 수동 복구하라",
                ) from e
            raise HTTPException(500, f"세계 역사 적재에 실패했다 (복원은 취소됨): {e}") from e
        return {"actor_id": persona_id, "name": name}

    return router
