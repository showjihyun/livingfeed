#!/usr/bin/env python
"""JSON Schema → Pydantic v2 모델 생성 (ADR-001: 타입은 전부 코드젠으로 파생).

실행 (저장소 루트에서):
    uv run --package lf-schemas python packages/schemas/scripts/generate.py

CI는 이 스크립트 실행 후 `git diff --exit-code` 로 드리프트를 검증한다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SCHEMAS_ROOT = Path(__file__).resolve().parents[1]
EVENTS_DIR = SCHEMAS_ROOT / "events"
PERMISSIONS_FILE = SCHEMAS_ROOT / "permissions.yaml"
PY_OUT = SCHEMAS_ROOT / "python" / "src" / "lf_schemas" / "generated"


def module_name(schema_file: Path) -> str:
    return schema_file.name.removesuffix(".schema.json").replace(".", "_").replace("-", "_")


def main() -> int:
    PY_OUT.mkdir(parents=True, exist_ok=True)
    modules: list[str] = []
    for schema in sorted(EVENTS_DIR.glob("*.schema.json")):
        module = module_name(schema)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input", str(schema),
                "--input-file-type", "jsonschema",
                "--output", str(PY_OUT / f"{module}.py"),
                "--output-model-type", "pydantic_v2.BaseModel",
                "--target-python-version", "3.12",
                "--disable-timestamp",
                "--use-schema-description",
                "--use-double-quotes",
            ],
            check=True,
        )
        modules.append(module)
        print(f"generated: {module}.py")

    init_lines = ['"""codegen 산출물 — 수동 편집 금지 (scripts/generate.py 로 재생성)."""\n']
    init_lines += [f"from lf_schemas.generated import {m} as {m}\n" for m in modules]
    (PY_OUT / "__init__.py").write_text("".join(init_lines), encoding="utf-8")

    # 런타임 검증용 원천 사본 — 설치된 패키지(휠/이미지)에서도 JSON Schema와
    # 권한 매트릭스에 접근할 수 있도록 패키지 데이터로 복사한다 (ADR-017 §2).
    # 드리프트는 CI의 generated/ diff 게이트가 잡는다.
    schemas_out = PY_OUT / "schemas"
    schemas_out.mkdir(exist_ok=True)
    for schema in sorted(EVENTS_DIR.glob("*.schema.json")):
        shutil.copyfile(schema, schemas_out / schema.name)
        print(f"copied: schemas/{schema.name}")
    shutil.copyfile(PERMISSIONS_FILE, schemas_out / "permissions.yaml")
    print("copied: schemas/permissions.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
