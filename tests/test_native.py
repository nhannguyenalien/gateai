import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import db
from app.main import account, app
from app.routing import route_for, upstream_payload
from app.settings import settings
from app.streaming import stream_chat


@pytest.fixture(autouse=True)
def provider(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "synthetic")


def test_native_tool_roundtrip_and_model_id():
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Find weather"}]},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "weather", "arguments": "{}"}}
                ],
                "reasoning_details": [{"text": "thinking"}],
            },
            {"role": "tool", "tool_call_id": "a", "content": "Sunny"},
        ],
        "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}],
        "max_tokens": 8000,
        "temperature": 0.2,
    }
    route = route_for("nvidia/nemotron-3-ultra-550b-a55b:free", "chat", body)
    sent = upstream_payload(body, route, "chat")
    assert sent["messages"] == body["messages"]
    assert sent["tools"] == body["tools"]
    assert sent["max_tokens"] == 8000
    assert sent["provider"] == {"require_parameters": True}


@pytest.mark.parametrize(
    "extra",
    [
        {"plugins": [{"id": "web"}]},
        {"models": ["paid"]},
        {"provider": {}},
        {"tools": [{"type": "web_search"}]},
        {"max_tokens": 32769},
        {"stream_options": True},
        {"n": 2},
    ],
)
def test_no_cost_policy_bypass(extra):
    with pytest.raises(HTTPException):
        route_for("chat-free", "chat", {"messages": [{"role": "user", "content": "hello"}], **extra})


def test_sdk_optional_idempotency_and_responses(aid, monkeypatch):
    import app.main as main

    calls = []
    monkeypatch.setattr(main, "submit", lambda j: calls.append(j) or {"id": "gen-test", "output": []})
    app.dependency_overrides[account] = lambda: {"id": aid}
    try:
        response = TestClient(app).post(
            "/v1/responses",
            json={
                "model": "chat-free",
                "input": [{"type": "function_call_output", "call_id": "a", "output": "Sunny"}],
                "store": False,
            },
        )
        assert response.status_code == 200
        assert response.headers["X-Job-ID"]
        assert calls[0]["kind"] == "responses"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("kind", ["chat", "responses"])
def test_native_stream_persistence(aid, monkeypatch, kind):
    tool = {
        "index": 0,
        "id": "call-1",
        "type": "function",
        "function": {"name": "weather", "arguments": '{"city":'},
    }
    chunks = [
        {"id": "gen-native", "choices": [{"delta": {"tool_calls": [tool]}}]},
        {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Hanoi"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    if kind == "responses":
        chunks = [
            {"type": "response.created", "response": {"id": "gen-native"}},
            {
                "type": "response.completed",
                "response": {
                    "id": "gen-native",
                    "status": "completed",
                    "output": [{"type": "function_call", "name": "weather", "arguments": "{}"}],
                    "usage": {"cost": 0},
                },
            },
        ]
    wire = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + (
        "data: [DONE]\n\n" if kind == "chat" else ""
    )
    sent = []

    def handler(req):
        sent.append((str(req.url), json.loads(req.content)))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.streaming.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )

    async def run():
        response = await stream_chat(
            {
                "model": "chat-free",
                "stream": True,
                **(
                    {"input": "Hello"}
                    if kind == "responses"
                    else {"messages": [{"role": "user", "content": "Hello"}]}
                ),
            },
            {"id": aid},
            "native-stream",
            kind=kind,
        )
        return "".join([c async for c in response.body_iterator])

    wire_out = asyncio.run(run())
    with db() as c:
        job = c.execute("SELECT * FROM jobs WHERE account_id=%s", (aid,)).fetchone()
    assert job["status"] == "succeeded"
    if kind == "chat":
        function = job["result"]["choices"][0]["message"]["tool_calls"][0]["function"]
        assert function == {"name": "weather", "arguments": '{"city":"Hanoi"}'}
    else:
        assert "event: response.completed\n" in wire_out
        assert job["result"]["output"][0]["name"] == "weather"
        assert sent[0][0].endswith("/responses")
        assert "stream_options" not in sent[0][1]


def test_embedding_sdk_encoding():
    route = route_for("embed-liquid-free", "embeddings", {"input": "Hello", "encoding_format": "base64"})
    assert (
        upstream_payload({"input": "Hello", "encoding_format": "base64"}, route, "embeddings")[
            "encoding_format"
        ]
        == "base64"
    )
