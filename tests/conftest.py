import uuid
from pathlib import Path
import pytest
import psycopg
from psycopg import sql
from app.settings import settings


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
