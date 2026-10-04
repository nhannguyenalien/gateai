import hashlib
import secrets
import uuid
from pathlib import Path

import redis
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.billing import finish, reserve
from app.db import db
from app.providers import submit
from app.routing import route_for
from app.settings import settings
from app.storage import signed

app = FastAPI(title="GateAI", version="0.1.0")
cache = redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2)


@app.middleware("http")
async def body_limit(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        size = 0
        chunks = []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
            chunks.append(chunk)
        request._body = b"".join(chunks)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def admin(authorization: str = Header(default="")):
    if not secrets.compare_digest(authorization, "Bearer " + settings.admin_key):
        raise HTTPException(401, "Invalid admin credential")


def account(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bearer API key required")
    digest = hashlib.sha256(authorization.removeprefix("Bearer ").encode()).hexdigest()
    with db() as c:
        row = c.execute(
            "SELECT a.* FROM api_keys k JOIN accounts a ON a.id=k.account_id WHERE k.hash=%s AND NOT k.revoked",
            (digest,),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Invalid API key")
    try:
        n = cache.eval(
            "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
            1,
            "rate:" + str(row["id"]),
        )
    except redis.RedisError:
        raise HTTPException(503, "Rate limiter unavailable")
    if n > row["rpm"]:
        raise HTTPException(429, "Rate limit exceeded")
    return row


class AccountInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    daily_cap_micros: int = Field(default=10_000_000, gt=0)
    rpm: int = Field(default=60, gt=0, le=10000)


class CreditInput(BaseModel):
    amount_micros: int = Field(gt=0, le=1_000_000_000_000)
    reference: str = Field(min_length=8, max_length=150)


class MediaInput(BaseModel):
    model: str
    input: dict


def public_job(j):
    return {
        k: j[k]
        for k in (
            "id",
            "kind",
            "alias",
            "provider",
            "status",
            "estimated_cost",
            "max_cost",
            "user_price",
            "retry_count",
            "result",
            "error",
            "created_at",
        )
    }


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with db() as c:
            c.execute("SELECT 1 FROM accounts LIMIT 1")
        cache.ping()
    except Exception:
        raise HTTPException(503, "Dependency unavailable")
    return {"status": "ready"}


@app.post("/admin/accounts", dependencies=[Depends(admin)])
def create_account(body: AccountInput):
    key = "gai_" + secrets.token_urlsafe(32)
    aid = uuid.uuid4()
    with db() as c:
        c.execute(
            "INSERT INTO accounts(id,name,daily_cap,rpm) VALUES (%s,%s,%s,%s)",
            (aid, body.name, body.daily_cap_micros, body.rpm),
        )
        c.execute(
            "INSERT INTO api_keys(hash,account_id) VALUES (%s,%s)",
            (hashlib.sha256(key.encode()).hexdigest(), aid),
        )
    return {"account_id": aid, "api_key": key}


@app.post("/admin/accounts/{aid}/credits", dependencies=[Depends(admin)])
def credit(aid: uuid.UUID, body: CreditInput):
    with db() as c:
        if not c.execute("SELECT id FROM accounts WHERE id=%s FOR UPDATE", (aid,)).fetchone():
            raise HTTPException(404, "Account not found")
        old = c.execute("SELECT * FROM transactions WHERE reference=%s", (body.reference,)).fetchone()
        if old:
            if old["account_id"] != aid or old["amount"] != body.amount_micros:
                raise HTTPException(409, "Reference conflict")
            return {"credited": False}
        c.execute(
            "INSERT INTO transactions(account_id,amount,kind,reference) VALUES (%s,%s,'topup',%s)",
            (aid, body.amount_micros, body.reference),
        )
        c.execute("UPDATE accounts SET balance=balance+%s WHERE id=%s", (body.amount_micros, aid))
    return {"credited": True}


@app.delete("/admin/accounts/{aid}/keys", dependencies=[Depends(admin)])
def revoke(aid: uuid.UUID):
    with db() as c:
        c.execute("UPDATE api_keys SET revoked=true WHERE account_id=%s", (aid,))
    return {"revoked": True}


@app.get("/v1/balance")
def balance(a=Depends(account)):
    return {"available_micros": a["balance"], "currency": "USD"}


@app.get("/v1/jobs/{jid}")
def get_job(jid: uuid.UUID, a=Depends(account)):
    with db() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=%s AND account_id=%s", (jid, a["id"])).fetchone()
    if not j:
        raise HTTPException(404, "Job not found")
    result = public_job(j)
    result["result"] = signed(result["result"])
    return result


@app.post("/v1/image", status_code=202)
@app.post("/v1/video", status_code=202)
@app.post("/v1/tts", status_code=202)
@app.post("/v1/stt", status_code=202)
@app.post("/v1/custom", status_code=202)
def media(
    request: Request,
    body: MediaInput,
    a=Depends(account),
    idempotency_key: str = Header(min_length=8, max_length=150),
):
    kind = request.url.path.rsplit("/", 1)[-1]
    if kind not in ("image", "video", "tts", "stt", "custom"):
        raise HTTPException(404, "Unknown endpoint")
    r = route_for(body.model, kind, body.input)
    j, _ = reserve(a["id"], idempotency_key, kind, body.model, body.input, r)
    try:
        cache.lpush("media:wake", str(j["id"]))
        cache.ltrim("media:wake", 0, 999)
    except redis.RedisError:
        pass  # Durable DB queue guarantees recovery even if notification is lost.
    return public_job(j)


def chat_request(body, kind, a, key):
    payload = dict(body)
    alias = payload.pop("model", None)
    r = route_for(alias, kind, payload)
    j, new = reserve(a["id"], key, kind, alias, payload, r)
    if not new:
        if j["status"] == "succeeded":
            return j["result"]
        return JSONResponse({"id": str(j["id"]), "status": j["status"]}, status_code=409)
    with db() as c:
        c.execute("UPDATE jobs SET status='submitting',updated_at=now() WHERE id=%s", (j["id"],))
    try:
        result = submit(j)
    except Exception:
        finish(j["id"], "needs_review", error="Provider outcome uncertain")
        raise HTTPException(
            502,
            {"job_id": str(j["id"]), "message": "Provider outcome uncertain; credit held for reconciliation"},
        )
    finish(j["id"], "succeeded", result)
    return result


@app.post("/v1/chat/completions")
def chat(body: dict, a=Depends(account), idempotency_key: str = Header(min_length=8, max_length=150)):
    return chat_request(body, "chat", a, idempotency_key)


@app.post("/v1/responses")
def responses(body: dict, a=Depends(account), idempotency_key: str = Header(min_length=8, max_length=150)):
    return chat_request(body, "responses", a, idempotency_key)


@app.get("/admin/stats", dependencies=[Depends(admin)])
def stats():
    with db() as c:
        summary = c.execute(
            "SELECT coalesce(sum(cost_upper_bound),0) AS cost_bound,coalesce(sum(revenue),0) AS revenue,count(*) AS jobs FROM usage WHERE created_at>=date_trunc('month',now())"
        ).fetchone()
        groups = c.execute(
            "SELECT provider,alias,count(*) AS jobs,sum(cost_upper_bound) AS cost_bound,sum(revenue) AS revenue FROM usage GROUP BY provider,alias"
        ).fetchall()
        queue = c.execute("SELECT status,count(*) AS count FROM jobs GROUP BY status").fetchall()
        budgets = c.execute(
            "SELECT scope,reserved FROM budgets WHERE day=(now() AT TIME ZONE 'UTC')::date"
        ).fetchall()
        recent = c.execute(
            "SELECT id,alias,status,created_at FROM jobs ORDER BY created_at DESC LIMIT 30"
        ).fetchall()
    return {"month": summary, "providers": groups, "queue": queue, "today_budgets": budgets, "recent": recent}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return Path("app/dashboard.html").read_text()
