"""Small cross-dialect read-session boundary for dashboard read models."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any, Protocol


class Result:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @staticmethod
    def _row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            key: (
                (value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc)
                .isoformat()
                if isinstance(value, datetime) else value
            )
            for key, value in dict(row).items()
        }

    def fetchone(self) -> dict[str, Any] | None:
        return self._row(self._cursor.fetchone())

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for row in self._cursor:
            normalized = self._row(row)
            if normalized is not None:
                yield normalized


class QuerySession(Protocol):
    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Result: ...
    def table_exists(self, table: str) -> bool: ...
    def columns(self, table: str) -> set[str]: ...
    def valid_session_predicate(self, trace_alias: str) -> str: ...
    def content_bearing_predicate(self, trace_alias: str) -> str: ...


_PYTHON_STRIP_CODEPOINTS = (
    0x0009, 0x000A, 0x000B, 0x000C, 0x000D,
    0x001C, 0x001D, 0x001E, 0x001F, 0x0020,
    0x0085, 0x00A0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
    0x2006, 0x2007, 0x2008, 0x2009, 0x200A,
    0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
)


def _content_bearing_sql(trace_alias: str, strip_sql: Callable[[str], str]) -> str:
    def has_non_whitespace(column: str) -> str:
        value = f"COALESCE({trace_alias}.{column},'')"
        return f"{strip_sql(value)}<>''"

    return (
        f"COALESCE({trace_alias}.error,'')='' AND "
        f"{has_non_whitespace('prompt_redacted')} AND "
        f"{has_non_whitespace('response_redacted')}"
    )


def _valid_utf8(value: object) -> int:
    if not isinstance(value, bytes):
        return 0
    try:
        value.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return 0
    return 1


class SQLiteSession:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        connection.create_function("verdict_valid_utf8", 1, _valid_utf8, deterministic=True)

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Result:
        return Result(self._connection.execute(query, params))

    def table_exists(self, table: str) -> bool:
        return bool(self.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone())

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.execute(f"PRAGMA table_info({table})")}

    def valid_session_predicate(self, trace_alias: str) -> str:
        value = f"{trace_alias}.session_id"
        return (
            f"typeof({value})='text' AND CASE WHEN "
            f"length(CAST({value} AS BLOB)) BETWEEN 1 AND 256 "
            f"AND instr({value},char(0))=0 THEN "
            f"verdict_valid_utf8(CAST({value} AS BLOB)) ELSE 0 END=1"
        )

    def content_bearing_predicate(self, trace_alias: str) -> str:
        whitespace = "char(" + ",".join(str(value) for value in _PYTHON_STRIP_CODEPOINTS) + ")"
        return _content_bearing_sql(trace_alias, lambda value: f"TRIM({value},{whitespace})")


class PostgresSession:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> Result:
        return Result(self._connection.execute(query.replace("?", "%s"), params))

    def table_exists(self, table: str) -> bool:
        return bool(self.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,),
        ).fetchone())

    def columns(self, table: str) -> set[str]:
        return {
            row["column_name"] for row in self.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ?", (table,),
            )
        }

    def valid_session_predicate(self, trace_alias: str) -> str:
        value = f"{trace_alias}.session_id"
        return f"{value} IS NOT NULL AND {value}<>'' AND octet_length({value})<=256"

    def content_bearing_predicate(self, trace_alias: str) -> str:
        whitespace = "||".join(f"chr({value})" for value in _PYTHON_STRIP_CODEPOINTS)
        return _content_bearing_sql(trace_alias, lambda value: f"BTRIM({value},{whitespace})")
