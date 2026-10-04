from app.routing import available_models, route_for
from app.settings import settings
from fastapi import HTTPException
import pytest


def test_callable_models_exclude_restricted_inkling(monkeypatch):
    monkeypatch.setattr(settings, 'openrouter_api_key', 'synthetic')
    data = available_models()
    assert data['object'] == 'list'
    models = {m['id']: m for m in data['data']}
    assert models['chat-free']['upstream_model'] == 'nvidia/nemotron-3-ultra-550b-a55b:free'
    assert models['chat-free']['streaming'] is True
    assert 'chat-inkling-free' not in models
    with pytest.raises(HTTPException) as exc:
        route_for('chat-inkling-free', 'chat', {'messages': [{'role': 'user', 'content': 'Hello'}]})
    assert exc.value.status_code == 400


def test_embedding_validation(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "synthetic")
    models = {m["id"]: m for m in available_models()["data"]}
    assert models["embed-liquid-free"]["dimensions"] == 1024
    assert models["embed-nemotron-free"]["endpoint"] == "/v1/embeddings"
    for value in ["hello", ["hello", "world"]]:
        assert route_for("embed-liquid-free", "embeddings", {"input": value})["free"]
    for value in ["", [], [""], [123], 123, ["hello"] * 17]:
        with pytest.raises(HTTPException):
            route_for("embed-liquid-free", "embeddings", {"input": value})
    with pytest.raises(HTTPException):
        route_for("embed-liquid-free", "chat", {"messages": []})


def test_embeddings_endpoint_idempotency(monkeypatch, aid):
    from fastapi.testclient import TestClient
    from app.main import app, account
    import app.main as main
    monkeypatch.setattr(settings, "openrouter_api_key", "synthetic")
    calls = []
    def submit(job):
        calls.append(job)
        return {"object": "list", "data": [{"index": 0, "embedding": [0.1]*1024}], "usage": {"cost": 0}}
    monkeypatch.setattr(main, "submit", submit)
    app.dependency_overrides[account] = lambda: {"id": aid}
    try:
        with TestClient(app) as client:
            body = {"model": "embed-liquid-free", "input": "A red pencil"}
            headers = {"Idempotency-Key": "embedding-test-01"}
            first = client.post("/v1/embeddings", json=body, headers=headers)
            second = client.post("/v1/embeddings", json=body, headers=headers)
            assert first.status_code == second.status_code == 200
            assert first.json() == second.json()
            assert len(calls) == 1
    finally:
        app.dependency_overrides.clear()
