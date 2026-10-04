from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg import sql

from app.settings import settings


@contextmanager
def db():
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        conn.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(settings.database_schema))
        )
        yield conn


if __name__ == "__main__":
    from pathlib import Path

    with db() as conn:
        conn.execute(Path("config/schema.sql").read_text())
    print("GateAI schema ready")
