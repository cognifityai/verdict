"""Shared dashboard storage URL classification."""

from __future__ import annotations

import re

_LIBPQ_PARAMETER = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\s*=")


def is_postgres_storage(value: str) -> bool:
    """Return whether *value* is a PostgreSQL URL or libpq conninfo string."""
    if value.startswith(("postgres://", "postgresql://")):
        return True
    if value.startswith(("sqlite:///", "/", "./", "../", "~")):
        return False
    try:
        from psycopg import ProgrammingError
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        # Keep libpq-shaped values out of SQLite even when the optional driver
        # is absent; storage construction will then report the missing extra.
        if value.rstrip().lower().endswith((".db", ".sqlite", ".sqlite3")):
            return False
        return _LIBPQ_PARAMETER.search(value) is not None
    try:
        conninfo_to_dict(value)
    except ProgrammingError:
        return False
    return True
