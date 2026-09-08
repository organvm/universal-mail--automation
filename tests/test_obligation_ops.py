import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from api.app import app
from core.obligation_workflow import reconcile


def test_private_obligations_require_auth_and_verified_artifact(tmp_path, monkeypatch):
    client = TestClient(app)
    monkeypatch.delenv("UMA_OPS_TOKEN", raising=False)
    assert client.get("/v1/ops/obligations").status_code == 503
    monkeypatch.setenv("UMA_OPS_TOKEN", "test-private-token")
    assert client.get("/v1/ops/obligations").status_code == 401
    artifact = reconcile([], now=datetime.now(timezone.utc), coverage_gaps=["not_observed"])
    path = tmp_path / "obligations.json"
    path.write_text(json.dumps(artifact))
    monkeypatch.setenv("UMA_OBLIGATIONS_PATH", str(path))
    headers = {"Authorization": "Bearer test-private-token"}
    response = client.get("/v1/ops/obligations", headers=headers)
    assert response.status_code == 200
    assert response.json()["metrics"]["coverage_gaps"] == 1
    artifact["metrics"]["coverage_gaps"] = 0
    path.write_text(json.dumps(artifact))
    assert client.get("/v1/ops/obligations", headers=headers).status_code == 503
    assert client.post("/v1/ops/obligations", headers=headers, json={}).status_code == 405
