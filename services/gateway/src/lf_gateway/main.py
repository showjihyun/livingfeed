"""스캐폴드 단계: 헬스체크만 제공한다.

SSE/WS 핸들러는 로드맵 7단계(Living Feed MVP)에서 구현된다 (ADR-010).
실행: uv run --package lf-gateway uvicorn lf_gateway.main:app --reload
"""

from fastapi import FastAPI

app = FastAPI(title="lf-gateway")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "lf-gateway"}
