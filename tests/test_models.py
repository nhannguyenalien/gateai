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
