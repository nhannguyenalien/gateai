import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.db import db
from app.billing import reserve, finish
from app.main import app, account
from app.routing import route_for
from app.providers import fal_url

ROUTE = {"provider": "fal", "max_cost_micros": 100, "user_price_micros": 200, "fixed": {}, "model": "fake"}


@pytest.fixture
def aid():
    aid = uuid.uuid4()
    with db() as c:
        c.execute(
            "INSERT INTO accounts(id,name,balance,daily_cap) VALUES (%s,%s,1000,100000)",
            (aid, "test-" + str(aid)),
        )
    yield aid
    with db() as c:
        c.execute("DELETE FROM usage WHERE job_id IN (SELECT id FROM jobs WHERE account_id=%s)", (aid,))
        c.execute("DELETE FROM transactions WHERE account_id=%s", (aid,))
        c.execute("DELETE FROM jobs WHERE account_id=%s", (aid,))
        c.execute("DELETE FROM api_keys WHERE account_id=%s", (aid,))
        c.execute("DELETE FROM accounts WHERE id=%s", (aid,))
        c.execute("DELETE FROM budgets WHERE scope=%s", (str(aid),))


def make(aid, key="same-key", payload=None):
    return reserve(aid, key, "image", "test", payload or {"prompt": "hello"}, ROUTE)


def test_concurrent_idempotency(aid):
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda _: make(aid), range(5)))
    assert sum(new for _, new in results) == 1
    assert len({j["id"] for j, _ in results}) == 1
    with db() as c:
        assert c.execute("SELECT balance FROM accounts WHERE id=%s", (aid,)).fetchone()["balance"] == 800


def test_no_overspend(aid):
    def call(i):
        try:
            make(aid, str(i))
            return True
        except HTTPException as e:
            assert e.status_code == 402
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(call, range(8))) == 5


def test_conflict_and_exactly_once_refund(aid):
    j, _ = make(aid)
    with pytest.raises(HTTPException) as e:
        make(aid, payload={"prompt": "different"})
    assert e.value.status_code == 409
    finish(j["id"], "failed")
    finish(j["id"], "failed")
    with db() as c:
        assert c.execute("SELECT balance FROM accounts WHERE id=%s", (aid,)).fetchone()["balance"] == 1000


def test_budget_rollback(aid):
    with db() as c:
        c.execute("UPDATE accounts SET daily_cap=50 WHERE id=%s", (aid,))
    with pytest.raises(HTTPException) as e:
        make(aid)
    assert e.value.status_code == 429
    with db() as c:
        assert c.execute("SELECT balance FROM accounts WHERE id=%s", (aid,)).fetchone()["balance"] == 1000
        assert not c.execute("SELECT * FROM jobs WHERE account_id=%s", (aid,)).fetchall()


def test_uncertain_holds_credit(aid):
    j, _ = make(aid)
    finish(j["id"], "needs_review")
    with db() as c:
        assert c.execute("SELECT balance FROM accounts WHERE id=%s", (aid,)).fetchone()["balance"] == 800


def test_tenant_isolation(aid):
    j, _ = make(aid)
    app.dependency_overrides[account] = lambda: {"id": uuid.uuid4()}
    try:
        assert TestClient(app).get("/v1/jobs/" + str(j["id"])).status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_disabled_route():
    with pytest.raises(HTTPException):
        route_for("image-standard", "image", {})


@pytest.mark.parametrize(
    "url",
    [
        "http://queue.fal.run/x",
        "https://evil.com/x",
        "https://queue.fal.run@evil.com/x",
        "https://queue.fal.run:444/x",
    ],
)
def test_provider_url_restricted(url):
    with pytest.raises(ValueError):
        fal_url(url)


def test_api_auth_and_body_limit():
    c = TestClient(app)
    assert c.get("/admin/stats").status_code == 401
    assert c.post("/v1/image", content="x" * 70000).status_code == 413
    assert c.get("/health/live").status_code == 200


def test_worker_submission_and_poll(aid, monkeypatch):
    import app.worker as worker

    j, _ = make(aid)
    calls = []
    monkeypatch.setattr(worker, "submit", lambda job: calls.append(job["id"]) or {"id": "provider-123"})
    monkeypatch.setattr(worker, "poll", lambda job: ("succeeded", {"output": "done"}))
    monkeypatch.setattr(worker, "archive", lambda result, jid: result)
    assert worker.tick()
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=%s", (j["id"],)).fetchone()
        assert row["status"] == "running"
        assert row["provider_ref"] == {"id": "provider-123"}
        c.execute("UPDATE jobs SET updated_at=now()-interval '10 seconds' WHERE id=%s", (j["id"],))
    assert worker.tick()
    with db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE id=%s", (j["id"],)).fetchone()["status"] == "succeeded"
        )
    assert len(calls) == 1


def test_worker_does_not_resubmit_uncertain_job(aid, monkeypatch):
    import app.worker as worker

    j, _ = make(aid)

    def timeout(job):
        raise TimeoutError()

    monkeypatch.setattr(worker, "submit", timeout)
    assert worker.tick()
    assert not worker.tick()
    with db() as c:
        assert (
            c.execute("SELECT status FROM jobs WHERE id=%s", (j["id"],)).fetchone()["status"]
            == "needs_review"
        )


def test_responses_endpoint_not_shadowed(monkeypatch):
    import app.main as main

    app.dependency_overrides[account] = lambda: {"id": uuid.uuid4()}
    monkeypatch.setattr(main, "chat_request", lambda body, kind, a, key: {"kind": kind})
    try:
        r = TestClient(app).post(
            "/v1/responses",
            json={"model": "responses-fast", "input": "hi"},
            headers={"Idempotency-Key": "request-123"},
        )
        assert r.status_code == 200
        assert r.json() == {"kind": "responses"}
    finally:
        app.dependency_overrides.clear()


def test_storage_rejects_untrusted_output(monkeypatch):
    from app.settings import settings
    from app.storage import archive

    monkeypatch.setattr(settings, "s3_bucket", "test-bucket")
    with pytest.raises(ValueError):
        archive({"url": "https://127.0.0.1/internal"}, uuid.uuid4())
