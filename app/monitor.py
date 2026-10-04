"""Durable cost reconciliation and privacy-safe Telegram operational alerts."""

import logging
import time
from decimal import Decimal, ROUND_CEILING
from datetime import timedelta

import httpx

from app.db import db
from app.settings import settings

log = logging.getLogger("monitor")


def record_cost(jid, generation_id, total_cost):
    value = Decimal(str(total_cost))
    if not value.is_finite() or value < 0:
        raise ValueError("Invalid provider cost")
    micros = int((value * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    with db() as c:
        c.execute("SELECT pg_advisory_xact_lock(741925)")
        j = c.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (jid,)).fetchone()
        matches = j and (
            (j["provider"] in ("openrouter", "litellm") and j["generation_id"] == generation_id)
            or (j["provider"] == "fal" and (j["provider_ref"] or {}).get("request_id") == generation_id)
        )
        if not matches:
            raise ValueError("Receipt does not match job")
        if j["actual_cost"] is not None:
            if j["actual_cost"] != micros:
                raise ValueError("Provider receipt changed; operator review required")
            return
        c.execute("UPDATE jobs SET actual_cost=%s,cost_checked_at=now() WHERE id=%s", (micros, jid))
        # Never release conservative reservations; raise them if actual billing exceeds the estimate.
        excess = max(0, micros - j["max_cost"])
        if excess:
            for scope in ["global", str(j["account_id"])]:
                c.execute(
                    "UPDATE budgets SET reserved=reserved+%s WHERE day=(%s AT TIME ZONE 'UTC')::date AND scope=%s",
                    (excess, j["created_at"], scope),
                )
            enqueue(
                c,
                "cost-overrun:" + str(jid),
                "GateAI: provider cost exceeded reserved estimate. Review job " + str(jid),
            )


def enqueue(c, key, message):
    c.execute("INSERT INTO alerts(key,message) VALUES (%s,%s) ON CONFLICT DO NOTHING", (key, message))


def reconcile(client):
    if not settings.openrouter_api_key:
        return
    with db() as c:
        rows = c.execute("""SELECT id,generation_id,provider,status,result FROM jobs WHERE generation_id IS NOT NULL
            AND actual_cost IS NULL AND provider IN ('openrouter','litellm')
            AND status IN ('succeeded','failed','needs_review')
            AND (cost_checked_at IS NULL OR cost_checked_at<now()-interval '5 minutes')
            ORDER BY cost_checked_at NULLS FIRST,created_at LIMIT 20""").fetchall()
    for row in rows:
        with db() as c:
            c.execute("UPDATE jobs SET cost_checked_at=now() WHERE id=%s", (row["id"],))
        try:
            result = row["result"] or {}
            usage = result.get("usage") or {}
            if (
                row["provider"] == "openrouter"
                and row["status"] == "succeeded"
                and result.get("id") == row["generation_id"]
                and usage.get("cost") is not None
            ):
                record_cost(row["id"], row["generation_id"], usage["cost"])
                continue
            res = client.get(
                "https://openrouter.ai/api/v1/generation",
                params={"id": row["generation_id"]},
                headers={"Authorization": "Bearer " + settings.openrouter_api_key},
            )
            res.raise_for_status()
            receipt = res.json()["data"]
            if receipt.get("id") != row["generation_id"] or receipt.get("total_cost") is None:
                raise ValueError("Missing matching receipt")
            record_cost(row["id"], row["generation_id"], receipt["total_cost"])
        except Exception:
            log.warning("Cost receipt pending for job %s", row["id"])


def reconcile_fal(client):
    if not settings.fal_key:
        return
    with db() as c:
        rows = c.execute("""SELECT * FROM jobs WHERE provider='fal' AND provider_ref->>'request_id' IS NOT NULL
            AND actual_cost IS NULL AND status IN ('succeeded','failed','needs_review')
            AND created_at>now()-interval '89 days'
            AND (cost_checked_at IS NULL OR cost_checked_at<now()-interval '5 minutes')
            ORDER BY cost_checked_at NULLS FIRST,created_at LIMIT 20""").fetchall()
    for row in rows:
        with db() as c:
            c.execute("UPDATE jobs SET cost_checked_at=now() WHERE id=%s", (row["id"],))
        try:
            rid = row["provider_ref"]["request_id"]
            response = client.get(
                "https://api.fal.ai/v1/models/billing-events",
                headers={"Authorization": "Key " + settings.fal_key},
                params={
                    "request_id": rid,
                    "start": (row["created_at"] - timedelta(minutes=5)).isoformat(),
                    "limit": 100,
                },
            )
            response.raise_for_status()
            data = response.json()
            receipts = data["billing_events"]
            # Ambiguous/multiple receipts remain pending for operator review.
            if data.get("has_more") or len(receipts) != 1:
                continue
            receipt = receipts[0]
            if receipt["request_id"] != rid or receipt["endpoint_id"] != row["route"]["model"]:
                raise ValueError("Mismatched receipt")
            record_cost(row["id"], rid, receipt["cost_total"])
        except Exception:
            log.warning("Fal cost receipt pending for job %s", row["id"])


def collect_alerts():
    with db() as c:
        day = c.execute("SELECT (now() AT TIME ZONE 'UTC')::date AS day").fetchone()["day"]
        budget = c.execute("SELECT reserved FROM budgets WHERE day=%s AND scope='global'", (day,)).fetchone()
        if budget and budget["reserved"] >= settings.global_daily_cap_micros * 0.8:
            enqueue(
                c,
                f"budget:{day}",
                f"GateAI: daily budget reached 80%. Reserved ${budget['reserved'] / 1e6:.2f} / ${settings.global_daily_cap_micros / 1e6:.2f} (UTC).",
            )
        rows = c.execute("""SELECT provider,count(*) AS total,
            count(*) FILTER (WHERE status IN ('failed','needs_review')) AS errors
            FROM jobs WHERE created_at>now()-interval '15 minutes' GROUP BY provider""").fetchall()
        hour = c.execute("SELECT to_char(now() AT TIME ZONE 'UTC','YYYY-MM-DD-HH24') AS hour").fetchone()[
            "hour"
        ]
        for r in rows:
            if r["errors"] >= 3 and r["errors"] / r["total"] >= 0.2:
                enqueue(
                    c,
                    f"errors:{r['provider']}:{hour}",
                    f"GateAI: {r['provider']} has {r['errors']}/{r['total']} failed or uncertain jobs in 15 minutes.",
                )


def deliver(client):
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    with db() as c:
        row = c.execute(
            "SELECT * FROM alerts WHERE sent_at IS NULL AND next_attempt<=now() ORDER BY next_attempt LIMIT 1 FOR UPDATE SKIP LOCKED"
        ).fetchone()
        if not row:
            return
        try:
            response = client.post(
                "https://api.telegram.org/bot" + settings.telegram_bot_token + "/sendMessage",
                json={"chat_id": settings.telegram_chat_id, "text": row["message"]},
            )
            response.raise_for_status()
            if not response.json().get("ok"):
                raise ValueError("Telegram rejected message")
            c.execute("UPDATE alerts SET sent_at=now(),attempts=attempts+1 WHERE key=%s", (row["key"],))
        except Exception:
            c.execute(
                "UPDATE alerts SET attempts=attempts+1,next_attempt=now()+interval '5 minutes' WHERE key=%s",
                (row["key"],),
            )
            log.warning("Telegram delivery pending")


def tick():
    with httpx.Client(timeout=15) as client:
        reconcile(client)
        reconcile_fal(client)
        collect_alerts()
        deliver(client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            tick()
        except Exception:
            log.warning("Monitor cycle failed; retrying")
        time.sleep(30)
