"""스캐폴드 단계: 헬스체크만 제공한다. 피드 조회는 로드맵 7단계에서 구현 (ADR-014)."""

from fastapi import FastAPI

app = FastAPI(title="lf-feed-api")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "lf-feed-api"}
