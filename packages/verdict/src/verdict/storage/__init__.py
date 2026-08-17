"""Storage adapters. The port is `Storage`; v0 ships SQLite + in-memory + Postgres.

Postgres is an optional install (`pip install "psycopg[binary,pool]"`);
the import is lazy so importing this module doesn't pull psycopg.
"""

from verdict.storage.base import Storage
from verdict.storage.buffered import BufferedStorage
from verdict.storage.memory import InMemoryStorage
from verdict.storage.sqlite import SQLiteStorage

__all__ = [
    "BufferedStorage",
    "InMemoryStorage",
    "PostgresStorage",
    "SQLiteStorage",
    "Storage",
]


def __getattr__(name: str):
    if name == "PostgresStorage":
        from verdict.storage.postgres import PostgresStorage
        return PostgresStorage
    raise AttributeError(name)
