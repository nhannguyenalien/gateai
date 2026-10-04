import uuid
from pathlib import Path
import pytest
import psycopg
from psycopg import sql
from app.settings import settings
from app.db import db


@pytest.fixture(scope="session", autouse=True)
def isolated_schema():
    original = settings.database_schema
    schema = "gateai_test_" + uuid.uuid4().hex
    settings.database_schema = schema
    with psycopg.connect(settings.database_url) as c:
        c.execute(Path("config/schema.sql").read_text().replace("gateai", schema))
    yield
    with psycopg.connect(settings.database_url) as c:
        c.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    settings.database_schema = original


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
