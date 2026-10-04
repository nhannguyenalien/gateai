import logging
import time
import redis
from app.settings import settings

from psycopg.types.json import Jsonb

from app.billing import finish
from app.db import db
from app.providers import poll, submit
from app.storage import archive

log = logging.getLogger("gateai.worker")


def tick():
    with db() as c:
        # Never resubmit uncertain paid calls after a crash. Reconcile manually.
        c.execute(
            "UPDATE jobs SET status='needs_review',error='Submission interrupted; reconcile provider before refund',updated_at=now() WHERE (status='submitting' OR (status='queued' AND kind IN ('chat','responses'))) AND updated_at<now()-interval '5 minutes'"
        )
        j = c.execute(
            "SELECT * FROM jobs WHERE (status='queued' AND kind NOT IN ('chat','responses')) OR (status='running' AND updated_at<now()-interval '5 seconds') ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1"
        ).fetchone()
        if not j:
            return False
        c.execute(
            "UPDATE jobs SET status=%s,updated_at=now() WHERE id=%s",
            ("submitting" if j["status"] == "queued" else "running", j["id"]),
        )
    try:
        if j["status"] == "queued":
            ref = submit(j)
            with db() as c:
                c.execute(
                    "UPDATE jobs SET status='running',provider_ref=%s,updated_at=now() WHERE id=%s",
                    (Jsonb(ref), j["id"]),
                )
        else:
            result = poll(j)
            if result:
                output = archive(result[1], j["id"]) if result[0] == "succeeded" else None
                finish(j["id"], result[0], output, "Provider failed" if result[0] == "failed" else None)
    except Exception:
        log.warning("Provider operation failed job=%s", j["id"])
        if j["status"] == "queued":
            finish(j["id"], "needs_review", error="Submission outcome uncertain; reconcile with provider")
        # Polling is safe to repeat; no repeat of billable submission.
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            if not tick():
                try:
                    redis.Redis.from_url(settings.redis_url, socket_timeout=6).brpop("media:wake", timeout=2)
                except redis.RedisError:
                    time.sleep(2)
        except Exception:
            log.warning("Worker database unavailable")
            time.sleep(5)
