"""Shared dashboard storage URL classification."""

from __future__ import annotations

import re

_LIBPQ_PARAMETER = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")
_LIBPQ_KEYS = frozenset(
    {
        "application_name",
        "channel_binding",
        "client_encoding",
        "connect_timeout",
        "dbname",
        "fallback_application_name",
        "gssencmode",
        "gsslib",
        "host",
        "hostaddr",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "krbsrvname",
        "load_balance_hosts",
        "options",
        "passfile",
        "password",
        "port",
        "replication",
        "requirepeer",
        "service",
        "sslcert",
        "sslcompression",
        "sslcrl",
        "sslcrldir",
        "sslkey",
        "ssl_max_protocol_version",
        "ssl_min_protocol_version",
        "sslmode",
        "sslpassword",
        "sslrootcert",
        "sslsni",
        "target_session_attrs",
        "tcp_user_timeout",
        "user",
    }
)


def _looks_like_libpq_conninfo(value: str) -> bool:
    match = _LIBPQ_PARAMETER.search(value)
    return match is not None and match.group(1).lower() in _LIBPQ_KEYS


def is_postgres_storage(value: str) -> bool:
    """Return whether *value* is a PostgreSQL URL or libpq conninfo string."""
    if value.startswith(("postgres://", "postgresql://")):
        return True
    if value.startswith(("sqlite:///", "/", "./", "../", "~")):
        return False
    if not _looks_like_libpq_conninfo(value):
        return False
    try:
        from psycopg import ProgrammingError
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        # Keep libpq-shaped values out of SQLite even when the optional driver
        # is absent; storage construction will then report the missing extra.
        return True
    try:
        conninfo_to_dict(value)
    except ProgrammingError:
        return False
    return True
