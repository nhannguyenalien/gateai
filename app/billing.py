import hashlib
import json
import uuid

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.db import db
from app.settings import settings


def reserve(account_id, key, kind, alias, payload, route):
    fingerprint = hashlib.sha256(json.dumps([kind, alias, payload], sort_keys=True).encode()).hexdigest()
    with db() as c:
        # One lock serializes global admission, including first budget row of the day.
        c.execute("SELECT pg_advisory_xact_lock(741925)")
        old = c.execute(
            "SELECT * FROM jobs WHERE account_id=%s AND idempotency_key=%s", (account_id, key)
        ).fetchone()
        if old:
            if old["request_hash"] != fingerprint:
                raise HTTPException(409, "Idempotency key reused with different input")
            return old, False
        account = c.execute("SELECT * FROM accounts WHERE id=%s FOR UPDATE", (account_id,)).fetchone()
        cost, price = route["max_cost_micros"], route["user_price_micros"]
        if account["balance"] < price:
            raise HTTPException(402, "Insufficient credits")
        for scope, cap in [
            ("global", settings.global_daily_cap_micros),
            (str(account_id), account["daily_cap"]),
        ]:
            c.execute(
                "INSERT INTO budgets(day,scope) VALUES ((now() AT TIME ZONE 'UTC')::date,%s) ON CONFLICT DO NOTHING",
                (scope,),
            )
            row = c.execute(
                "UPDATE budgets SET reserved=reserved+%s WHERE day=(now() AT TIME ZONE 'UTC')::date AND scope=%s AND reserved+%s<=%s RETURNING reserved",
                (cost, scope, cost, cap),
            ).fetchone()
            if not row:
                raise HTTPException(429, "Daily cost budget exceeded")
        jid = uuid.uuid4()
        c.execute("UPDATE accounts SET balance=balance-%s WHERE id=%s", (price, account_id))
        c.execute(
            "INSERT INTO transactions(account_id,job_id,amount,kind) VALUES (%s,NULL,%s,'reserve')",
            (account_id, -price),
        )
        job = c.execute(
            """INSERT INTO jobs(id,account_id,idempotency_key,request_hash,kind,alias,provider,route,payload,estimated_cost,max_cost,user_price)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                jid,
                account_id,
                key,
                fingerprint,
                kind,
                alias,
                route["provider"],
                Jsonb(route),
                Jsonb(payload),
                cost,
                cost,
                price,
            ),
        ).fetchone()
        c.execute(
            "UPDATE transactions SET job_id=%s WHERE id=currval(pg_get_serial_sequence('transactions','id'))",
            (jid,),
        )
        return job, True


def finish(jid, status, result=None, error=None):
    with db() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (jid,)).fetchone()
        if j["status"] in ("succeeded", "failed"):
            return
        c.execute(
            "UPDATE jobs SET status=%s,result=%s,error=%s,updated_at=now() WHERE id=%s",
            (status, Jsonb(result), error, jid),
        )
        if status == "failed":
            c.execute(
                "UPDATE accounts SET balance=balance+%s WHERE id=%s", (j["user_price"], j["account_id"])
            )
            c.execute(
                "INSERT INTO transactions(account_id,job_id,amount,kind) VALUES (%s,%s,%s,'refund')",
                (j["account_id"], jid, j["user_price"]),
            )
        if status in ("succeeded", "failed"):
            # Retain daily cost reservation even on failure: vendor may still bill.
            c.execute(
                "INSERT INTO usage(job_id,provider,alias,cost_upper_bound,revenue) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    jid,
                    j["provider"],
                    j["alias"],
                    j["max_cost"],
                    j["user_price"] if status == "succeeded" else 0,
                ),
            )
