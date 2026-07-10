"""Living Feed 공유 스키마 패키지.

모든 모델은 packages/schemas/events/*.schema.json 에서 코드젠으로 생성된다 (ADR-001).
수동 모델 작성 금지 — 재생성: uv run --package lf-schemas python packages/schemas/scripts/generate.py
"""

from lf_schemas.generated.envelope import EventEnvelope as EventEnvelope
