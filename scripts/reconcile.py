"""Operator-only reconciliation after checking the provider dashboard/invoice."""

import argparse
from app.billing import finish
from app.db import db

p = argparse.ArgumentParser()
p.add_argument("job_id")
p.add_argument("--outcome", choices=["succeeded", "failed"], required=True)
p.add_argument("--note", required=True)
a = p.parse_args()
with db() as c:
    j = c.execute("SELECT status FROM jobs WHERE id=%s", (a.job_id,)).fetchone()
    if not j or j["status"] != "needs_review":
        raise SystemExit("Only needs_review jobs may be reconciled")
finish(a.job_id, a.outcome, error="Operator reconciliation: " + a.note)
print("Reconciled")
