import asyncio
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException

from app.billing import reserve
from app.db import db
from app.monitor import collect_alerts, deliver, enqueue, record_cost
from app.settings import settings
from app.streaming import events, save_generation, stream_chat


@pytest.fixture
def free(monkeypatch):
    monkeypatch.setattr(settings, "openrouter_api_key", "test-provider-key")
    return {"model": "chat-free", "messages": [{"role": "user", "content": "Say hello"}], "stream": True}


def test_sse_multiline():
    async def lines():
        for line in [": heartbeat", "", 'data: {"a":', "data: 1}", "", "data: [DONE]", ""]:
            yield line

    async def run():
        return [e async for e in events(lines())]

    assert asyncio.run(run()) == ['{"a":\n1}', "[DONE]"]


@pytest.mark.parametrize(
    "ending,status",
    [("[DONE]", "succeeded"), ("", "needs_review"), ('{"error":{"message":"broken"}}', "needs_review")],
)
def test_stream_lifecycle(aid, free, monkeypatch, ending, status):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        body = (
            "data: "
            + json.dumps(
                {"id": "gen-test", "choices": [{"delta": {"content": "Hello"}, "finish_reason": "stop"}]}
            )
            + "\n\n"
        )
        if ending:
            body += "data: " + ending + "\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "app.streaming.httpx.AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )

    async def run():
        response = await stream_chat(free, {"id": aid}, "stream-test-key")
        chunks = [chunk async for chunk in response.body_iterator]
        repeated = await stream_chat(free, {"id": aid}, "stream-test-key")
        assert repeated.status_code == 409
        return chunks

    chunks = asyncio.run(run())
    with db() as c:
        job = c.execute("SELECT * FROM jobs WHERE account_id=%s", (aid,)).fetchone()
        assert job["status"] == status
        assert job["generation_id"] == "gen-test"
        assert job["actual_cost"] is None
    assert len(sent) == 1 and sent[0]["stream"] is True
    assert ("data: [DONE]\n\n" in chunks) == (status == "succeeded")


def test_receipt_zero_and_idempotency(aid):
    r = {"provider": "openrouter", "max_cost_micros": 0, "user_price_micros": 0}
    job, _ = reserve(aid, "receipt-test", "chat", "free", {}, r)
    save_generation(job["id"], "gen-receipt")
    with pytest.raises(ValueError):
        record_cost(job["id"], "gen-wrong", 0)
    record_cost(job["id"], "gen-receipt", 0)
    record_cost(job["id"], "gen-receipt", 0)
    with db() as c:
        assert (
            c.execute("SELECT actual_cost FROM jobs WHERE id=%s", (job["id"],)).fetchone()["actual_cost"] == 0
        )
    with pytest.raises(ValueError):
        record_cost(job["id"], "gen-receipt", ".1")


def test_overrun_blocks_next_admission(aid, monkeypatch):
    r = {"provider": "openrouter", "max_cost_micros": 1, "user_price_micros": 2}
    with db() as c:
        c.execute("DELETE FROM budgets WHERE scope='global'")
    monkeypatch.setattr(settings, "global_daily_cap_micros", 10)
    job, _ = reserve(aid, "overrun-test", "chat", "test", {}, r)
    save_generation(job["id"], "gen-overrun")
    record_cost(job["id"], "gen-overrun", "0.000011")
    with pytest.raises(HTTPException) as e:
        reserve(aid, "second-job", "chat", "test", {}, r)
    assert e.value.status_code == 429
    collect_alerts()
    collect_alerts()
    with db() as c:
        assert c.execute("SELECT count(*) AS n FROM alerts WHERE key LIKE 'budget:%'").fetchone()["n"] == 1
        c.execute("DELETE FROM budgets WHERE scope='global'")
        c.execute("DELETE FROM alerts")


def test_telegram_retry_and_dedup(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", "test-chat")
    key = str(uuid.uuid4())
    with db() as c:
        c.execute("DELETE FROM alerts")
        enqueue(c, key, "Synthetic alert")
        enqueue(c, key, "Synthetic alert")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client:
        deliver(client)
    with db() as c:
        row = c.execute("SELECT * FROM alerts WHERE key=%s", (key,)).fetchone()
        assert row["attempts"] == 1 and row["sent_at"] is None
        c.execute("UPDATE alerts SET next_attempt=now() WHERE key=%s", (key,))
    calls = []

    def success(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(success)) as client:
        deliver(client)
        deliver(client)
    assert len(calls) == 1
    with db() as c:
        c.execute("DELETE FROM alerts WHERE key=%s", (key,))


def test_fal_receipt_matching(aid, monkeypatch):
    from app.monitor import reconcile_fal
    from psycopg.types.json import Jsonb

    monkeypatch.setattr(settings, "fal_key", "synthetic")
    route = {
        "provider": "fal",
        "model": "fal-ai/flux/schnell",
        "max_cost_micros": 10,
        "user_price_micros": 20,
    }
    job, _ = reserve(aid, "fal-receipt", "image", "image-standard", {}, route)
    with db() as c:
        c.execute(
            "UPDATE jobs SET status='succeeded',provider_ref=%s WHERE id=%s",
            (Jsonb({"request_id": "synthetic-request"}), job["id"]),
        )

    def handler(request):
        assert request.url.params["request_id"] == "synthetic-request"
        return httpx.Response(
            200,
            json={
                "has_more": False,
                "billing_events": [
                    {"request_id": "synthetic-request", "endpoint_id": route["model"], "cost_total": 0.000009}
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        reconcile_fal(client)
    with db() as c:
        assert (
            c.execute("SELECT actual_cost FROM jobs WHERE id=%s", (job["id"],)).fetchone()["actual_cost"] == 9
        )


def test_stream_disconnect_retains_reservation(aid, free, monkeypatch):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content='data: {"id":"gen-disconnect","choices":[{"delta":{"content":"Hello"}}]}\n\n',
        )
    )
    monkeypatch.setattr("app.streaming.httpx.AsyncClient", lambda **kw: original(transport=transport, **kw))

    async def run():
        response = await stream_chat(free, {"id": aid}, "disconnect-key")
        await anext(response.body_iterator)
        await response.body_iterator.aclose()

    asyncio.run(run())
    with db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE account_id=%s", (aid,)).fetchone()["status"]
            == "needs_review"
        )
