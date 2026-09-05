from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


def validate_test_dsn(
    dsn: str | None, *, allow_any_database: bool
) -> tuple[str | None, str]:
    if not dsn:
        return None, "VERDICT_TEST_POSTGRES_DSN is not set"
    try:
        database = conninfo_to_dict(dsn).get("dbname", "")
    except Exception:
        return None, "VERDICT_TEST_POSTGRES_DSN is not a valid PostgreSQL DSN"
    if allow_any_database or "test" in database.lower():
        return dsn, ""
    return None, "live PostgreSQL tests require a database name containing 'test'"


@contextmanager
def isolated_test_dsn(dsn: str) -> Iterator[str]:
    """Yield a private schema on a database already approved for test use."""
    schema = f"verdict_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield make_conninfo(dsn, options=f"-csearch_path={schema}")
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )
