from fastapi.testclient import TestClient
from lf_gateway.main import app


def test_healthz() -> None:
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "lf-gateway"}
