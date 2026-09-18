"""PostgreSQL implementation for the opt-in Fleet ReadPort V1 boundary."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from verdict.fleet_read_port import (
    VERDICT_FLEET_READ_SCHEMA_VERSION,
    TraceContributionReadV1,
    TraceWindowReadV1,
    VerdictFleetReadError,
    _tenant,
    trace_window_read_to_json,
)

_SCOPE_TABLE = "verdict_fleet_reader_scope_v1"
_TRACE_VIEW = "verdict_fleet_traces_v1"
_AGENT_VIEW = "verdict_fleet_agent_links_v1"
_TRACE_INDEX = "verdict_fleet_traces_tenant_started_trace_v1"
_LEDGER_PREFIX = "verdict_fleet_read_v1:"
_MAX_READERS = 8
_MAX_ITEMS = 2_000
_READ_DEADLINE_SECONDS = 2.0
_POOL_ACQUIRE_SECONDS = 0.25
_STATEMENT_TIMEOUT_MS = 1_500
_PREPARE_DEADLINE_SECONDS = 300.0
_LOCK_DEADLINE_SECONDS = 2.0

_TRACE_VIEW_COLUMNS = (
    "tenant_id",
    "trace_id",
    "started_at",
    "ended_at",
    "error_present",
    "provider",
    "request_model",
    "response_model",
    "service_name",
    "environment",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "cost_usd",
    "parent_span_id",
)
_AGENT_VIEW_COLUMNS = (
    "tenant_id",
    "trace_id",
    "link_count",
    "agent_event_id",
    "agent_turn_id",
    "agent_run_id",
)
_TRACE_VIEW_DEFINITION = """SELECT t.tenant_id,
    t.trace_id,
    t.started_at,
    t.ended_at,
    t.error IS NOT NULL AS error_present,
    t.provider,
    t.request_model,
    t.response_model,
    t.service_name,
    t.environment,
    t.input_tokens,
    t.output_tokens,
    t.latency_ms,
    t.cost_usd,
    t.parent_span_id
   FROM traces t
     JOIN verdict_fleet_reader_scope_v1 s ON s.tenant_id = t.tenant_id
  WHERE s.reader_role = SESSION_USER;"""
_AGENT_VIEW_DEFINITION = """SELECT e.tenant_id,
    e.trace_id,
    count(*) AS link_count,
        CASE
            WHEN count(*) = 1 THEN min(e.event_id)
            ELSE NULL::text
        END AS agent_event_id,
        CASE
            WHEN count(*) = 1 THEN min(e.turn_id)
            ELSE NULL::text
        END AS agent_turn_id,
        CASE
            WHEN count(*) = 1 THEN min(e.run_id)
            ELSE NULL::text
        END AS agent_run_id
   FROM agent_events e
     JOIN verdict_fleet_reader_scope_v1 s ON s.tenant_id = e.tenant_id
  WHERE s.reader_role = SESSION_USER AND e.trace_id IS NOT NULL
  GROUP BY e.tenant_id, e.trace_id;"""
_SCOPE_COLUMNS = (
    ("reader_role", "text", True),
    ("tenant_id", "text", True),
)
_SCOPE_CONSTRAINTS = (
    ("c", "CHECK (octet_length(reader_role) >= 1 AND octet_length(reader_role) <= 63)"),
    (
        "c",
        "CHECK (tenant_id <> 'local'::text AND tenant_id ~ "
        "'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'::text)",
    ),
    ("p", "PRIMARY KEY (reader_role)"),
    ("u", "UNIQUE (tenant_id)"),
)
_REQUIRED_BASE_COLUMNS = {
    "traces": {
        "tenant_id": "text",
        "trace_id": "text",
        "started_at": "timestamp with time zone",
        "ended_at": "timestamp with time zone",
        "error": "text",
        "provider": "text",
        "request_model": "text",
        "response_model": "text",
        "service_name": "text",
        "environment": "text",
        "input_tokens": "integer",
        "output_tokens": "integer",
        "latency_ms": "double precision",
        "cost_usd": "double precision",
        "parent_span_id": "text",
    },
    "agent_events": {
        "tenant_id": "text",
        "trace_id": "text",
        "event_id": "text",
        "turn_id": "text",
        "run_id": "text",
    },
    "verdict_schema_migrations": {
        "name": "text",
        "applied_at": "timestamp with time zone",
    },
}
_DEFINITION_CHECKSUM = hashlib.sha256(
    repr(
        (
            _SCOPE_COLUMNS,
            _SCOPE_CONSTRAINTS,
            _TRACE_VIEW_COLUMNS,
            _TRACE_VIEW_DEFINITION,
            _AGENT_VIEW_COLUMNS,
            _AGENT_VIEW_DEFINITION,
            ("tenant_id", "started_at", "trace_id"),
        )
    ).encode("utf-8")
).hexdigest()
_LEDGER_NAME = _LEDGER_PREFIX + _DEFINITION_CHECKSUM


class FleetPostgresConfigurationError(RuntimeError):
    pass


def _remaining_statement_ms(deadline: float) -> int:
    remaining = math.floor((deadline - time.monotonic()) * 1_000)
    if remaining < 1:
        raise FleetPostgresConfigurationError("fleet_deadline_exceeded")
    return remaining


class _DeadlineCursor:
    def __init__(self, cursor, deadline: float) -> None:
        self._cursor = cursor
        self._deadline = deadline

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def execute(self, query, params=None):
        remaining_ms = _remaining_statement_ms(self._deadline)
        self._cursor.execute(
            "SELECT set_config('statement_timeout',%s,false)",
            (str(remaining_ms),),
        )
        self._cursor.execute(query, params)
        return self

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class _DeadlineConnection:
    def __init__(self, connection, deadline: float) -> None:
        self._connection = connection
        self._deadline = deadline

    def execute(self, query, params=None):
        cursor = self.cursor()
        return cursor.execute(query, params)

    def cursor(self) -> _DeadlineCursor:
        return _DeadlineCursor(self._connection.cursor(), self._deadline)

    def transaction(self):
        return self._connection.transaction()


def _normalized_sql(value: str) -> str:
    return " ".join(value.split())


def _validate_reader_role(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("reader role must be bounded text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("reader role must be bounded text") from None
    if len(encoded) > 63:
        raise ValueError("reader role must be bounded text")
    return value


def _validate_database_url(value: object) -> str:
    if not isinstance(value, str) or not value.startswith(("postgres://", "postgresql://")):
        raise VerdictFleetReadError("unsupported_backend")
    return value


@dataclass(frozen=True)
class _Snapshot:
    shared_presence: tuple[bool, bool, bool]
    scope_valid: bool
    trace_view_valid: bool
    agent_view_valid: bool
    index_present: bool
    index_valid: bool
    ledger_rows: tuple[str, ...]
    scope_rows: tuple[tuple[str, str], ...]
    acl_rows: tuple[tuple[str, str, str, bool], ...]


def _schema_context(connection) -> tuple[str, str]:
    row = connection.execute(
        """SELECT current_schemas(false),session_user,current_user,
                  current_setting('server_version_num')::integer"""
    ).fetchone()
    if row is None:
        raise FleetPostgresConfigurationError("fleet_database_invalid")
    schemas, session_user, current_user, server_version = row
    if (
        not isinstance(schemas, list)
        or len(schemas) != 1
        or not isinstance(schemas[0], str)
        or session_user != current_user
        or not 160000 <= server_version < 170000
    ):
        raise FleetPostgresConfigurationError("fleet_database_invalid")
    schema = schemas[0]
    owner = connection.execute(
        """SELECT pg_get_userbyid(nspowner)
           FROM pg_namespace WHERE nspname=%s""",
        (schema,),
    ).fetchone()
    if owner is None or owner[0] != current_user:
        raise FleetPostgresConfigurationError("fleet_database_invalid")
    return schema, current_user


def _validate_base_schema(connection, schema: str, owner: str) -> None:
    rows = connection.execute(
        """SELECT table_name,column_name,data_type
           FROM information_schema.columns
           WHERE table_schema=%s AND table_name=ANY(%s)
           ORDER BY table_name,ordinal_position""",
        (schema, list(_REQUIRED_BASE_COLUMNS)),
    ).fetchall()
    actual: dict[str, dict[str, str]] = {name: {} for name in _REQUIRED_BASE_COLUMNS}
    for table_name, column_name, data_type in rows:
        actual.setdefault(table_name, {})[column_name] = data_type
    if any(
        actual.get(table_name, {}).get(column_name) != data_type
        for table_name, columns in _REQUIRED_BASE_COLUMNS.items()
        for column_name, data_type in columns.items()
    ):
        raise FleetPostgresConfigurationError("fleet_schema_unsupported")
    owners = connection.execute(
        """SELECT c.relname,pg_get_userbyid(c.relowner)
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=%s AND c.relname=ANY(%s)""",
        (schema, list(_REQUIRED_BASE_COLUMNS)),
    ).fetchall()
    if len(owners) != len(_REQUIRED_BASE_COLUMNS) or any(
        row_owner != owner for _, row_owner in owners
    ):
        raise FleetPostgresConfigurationError("fleet_schema_unsupported")


def _relation(connection, schema: str, name: str) -> tuple[Any, ...] | None:
    return connection.execute(
        """SELECT c.oid,c.relkind,pg_get_userbyid(c.relowner),c.reloptions
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname=%s AND c.relname=%s""",
        (schema, name),
    ).fetchone()


def _columns(connection, schema: str, name: str) -> tuple[str, ...]:
    return tuple(
        row[0]
        for row in connection.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""",
            (schema, name),
        ).fetchall()
    )


def _scope_is_valid(connection, schema: str, owner: str, relation) -> bool:
    if relation is None or relation[1:] != ("r", owner, None):
        return False
    columns = tuple(
        row
        for row in connection.execute(
            """SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull
               FROM pg_attribute a
               WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped
               ORDER BY a.attnum""",
            (relation[0],),
        ).fetchall()
    )
    constraints = tuple(
        (row[0], _normalized_sql(row[1]))
        for row in connection.execute(
            """SELECT contype,pg_get_constraintdef(oid,true)
               FROM pg_constraint WHERE conrelid=%s
               ORDER BY contype,pg_get_constraintdef(oid,true)""",
            (relation[0],),
        ).fetchall()
    )
    return columns == _SCOPE_COLUMNS and constraints == tuple(sorted(_SCOPE_CONSTRAINTS))


def _view_is_valid(
    connection,
    schema: str,
    owner: str,
    relation,
    columns: tuple[str, ...],
    definition: str,
) -> bool:
    if relation is None or relation[1] != "v" or relation[2] != owner:
        return False
    if sorted(relation[3] or []) != ["security_barrier=true"]:
        return False
    row = connection.execute("SELECT pg_get_viewdef(%s,true)", (relation[0],)).fetchone()
    if row is None or not isinstance(row[0], str):
        return False
    return _columns(connection, schema, relation_name(connection, relation[0])) == columns and (
        _normalized_sql(row[0]) == _normalized_sql(definition)
    )


def relation_name(connection, oid: int) -> str:
    row = connection.execute("SELECT relname FROM pg_class WHERE oid=%s", (oid,)).fetchone()
    if row is None or not isinstance(row[0], str):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    return row[0]


def _index_state(connection, schema: str) -> tuple[bool, bool]:
    row = connection.execute(
        """SELECT i.indisvalid,i.indisready,i.indisunique,i.indisprimary,
                  am.amname,pg_get_expr(i.indpred,i.indrelid),i.indnkeyatts,i.indnatts,
                  pg_get_userbyid(index_class.relowner)=pg_get_userbyid(table_class.relowner),
                  array_agg(a.attname ORDER BY keys.ordinality),
                  array_agg(i.indoption[keys.ordinality-1] ORDER BY keys.ordinality),
                  array_agg(opclass.opcname ORDER BY keys.ordinality)
           FROM pg_class index_class
           JOIN pg_namespace n ON n.oid=index_class.relnamespace
           JOIN pg_index i ON i.indexrelid=index_class.oid
           JOIN pg_class table_class ON table_class.oid=i.indrelid
           JOIN pg_namespace table_namespace ON table_namespace.oid=table_class.relnamespace
           JOIN pg_am am ON am.oid=index_class.relam
           JOIN unnest(i.indkey) WITH ORDINALITY AS keys(attnum,ordinality) ON true
           JOIN pg_attribute a ON a.attrelid=table_class.oid AND a.attnum=keys.attnum
           JOIN pg_opclass opclass ON opclass.oid=i.indclass[keys.ordinality-1]
           WHERE n.nspname=%s AND index_class.relname=%s
             AND table_namespace.nspname=%s AND table_class.relname='traces'
           GROUP BY i.indisvalid,i.indisready,i.indisunique,i.indisprimary,
                    am.amname,i.indpred,i.indrelid,i.indnkeyatts,i.indnatts,
                    index_class.relowner,table_class.relowner""",
        (schema, _TRACE_INDEX, schema),
    ).fetchone()
    if row is None:
        wrong = connection.execute(
            """SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=%s AND c.relname=%s""",
            (schema, _TRACE_INDEX),
        ).fetchone()
        return wrong is not None, False
    (
        valid,
        ready,
        unique,
        primary,
        access_method,
        predicate,
        key_count,
        attribute_count,
        owner_matches,
        columns,
        options,
        operator_classes,
    ) = row
    return True, bool(
        valid
        and ready
        and not unique
        and not primary
        and access_method == "btree"
        and predicate is None
        and key_count == 3
        and attribute_count == 3
        and owner_matches
        and columns == ["tenant_id", "started_at", "trace_id"]
        and options == [0, 0, 0]
        and operator_classes == ["text_ops", "timestamptz_ops", "text_ops"]
    )


def _acl_rows(connection, relation_oids: tuple[int, int]) -> tuple[tuple[str, str, str, bool], ...]:
    if (
        connection.execute(
            "SELECT 1 FROM pg_attribute WHERE attrelid=ANY(%s) "
            "AND attnum>0 AND attacl IS NOT NULL LIMIT 1",
            (list(relation_oids),),
        ).fetchone()
        is not None
    ):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    rows = connection.execute(
        """SELECT c.relname,COALESCE(pg_get_userbyid(acl.grantee),'PUBLIC'),
                  acl.privilege_type,acl.is_grantable
           FROM pg_class c
           CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) acl
           WHERE c.oid=ANY(%s) AND acl.grantee<>c.relowner
           ORDER BY c.relname,2,3 LIMIT 19""",
        (list(relation_oids),),
    ).fetchall()
    if len(rows) > 18:
        raise FleetPostgresConfigurationError("fleet_reader_limit_exceeded")
    return tuple(rows)


def _snapshot(connection, schema: str, owner: str) -> _Snapshot:
    scope = _relation(connection, schema, _SCOPE_TABLE)
    trace_view = _relation(connection, schema, _TRACE_VIEW)
    agent_view = _relation(connection, schema, _AGENT_VIEW)
    presence = (scope is not None, trace_view is not None, agent_view is not None)
    scope_valid = _scope_is_valid(connection, schema, owner, scope) if scope is not None else False
    trace_valid = (
        _view_is_valid(
            connection,
            schema,
            owner,
            trace_view,
            _TRACE_VIEW_COLUMNS,
            _TRACE_VIEW_DEFINITION,
        )
        if trace_view is not None
        else False
    )
    agent_valid = (
        _view_is_valid(
            connection,
            schema,
            owner,
            agent_view,
            _AGENT_VIEW_COLUMNS,
            _AGENT_VIEW_DEFINITION,
        )
        if agent_view is not None
        else False
    )
    index_present, index_valid = _index_state(connection, schema)
    ledger = tuple(
        row[0]
        for row in connection.execute(
            """SELECT name FROM verdict_schema_migrations
               WHERE name LIKE %s ORDER BY name LIMIT 2""",
            (_LEDGER_PREFIX + "%",),
        ).fetchall()
    )
    if len(ledger) > 1:
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    scope_rows: tuple[tuple[str, str], ...] = ()
    acl_rows: tuple[tuple[str, str, str, bool], ...] = ()
    if all(presence):
        scope_rows = tuple(
            connection.execute(
                f"SELECT reader_role,tenant_id FROM {_SCOPE_TABLE} "  # nosec B608
                "ORDER BY reader_role LIMIT 9"
            ).fetchall()
        )
        if len(scope_rows) > _MAX_READERS:
            raise FleetPostgresConfigurationError("fleet_reader_limit_exceeded")
        assert trace_view is not None and agent_view is not None
        acl_rows = _acl_rows(connection, (trace_view[0], agent_view[0]))
    return _Snapshot(
        shared_presence=presence,
        scope_valid=scope_valid,
        trace_view_valid=trace_valid,
        agent_view_valid=agent_valid,
        index_present=index_present,
        index_valid=index_valid,
        ledger_rows=ledger,
        scope_rows=scope_rows,
        acl_rows=acl_rows,
    )


def _validate_shared(snapshot: _Snapshot) -> None:
    if snapshot.shared_presence == (False, False, False):
        if snapshot.index_present or snapshot.ledger_rows:
            raise FleetPostgresConfigurationError("fleet_schema_invalid")
        return
    if snapshot.shared_presence != (True, True, True):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    if not (snapshot.scope_valid and snapshot.trace_view_valid and snapshot.agent_view_valid):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    if snapshot.ledger_rows and snapshot.ledger_rows != (_LEDGER_NAME,):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    if snapshot.ledger_rows and not snapshot.index_valid:
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    if not snapshot.ledger_rows and (snapshot.scope_rows or snapshot.acl_rows):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")


def _expected_acl(
    scope_rows: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str, str, bool], ...]:
    return tuple(
        sorted(
            (view, role, "SELECT", False)
            for role, _ in scope_rows
            for view in (_TRACE_VIEW, _AGENT_VIEW)
        )
    )


def _has_unauthorized_relation_privileges(executor, schema: str, role: str, oid: int) -> bool:
    row = executor.execute(
        """SELECT EXISTS(
             SELECT 1
             FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m','f')
               AND c.relname<>ALL(%s)
               AND (
                 has_table_privilege(
                   %s,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                 OR has_any_column_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
               )
           ) OR EXISTS(
             SELECT 1
             FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname=%s
               AND CASE WHEN c.relkind='S'
                 THEN has_sequence_privilege(%s,c.oid,'USAGE,SELECT,UPDATE')
                 ELSE false
               END
           ) OR EXISTS(
             SELECT 1
             FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             JOIN pg_attribute a ON a.attrelid=c.oid
             CROSS JOIN LATERAL aclexplode(a.attacl) acl
             WHERE n.nspname=%s AND acl.grantee=%s
           )""",
        (
            schema,
            [_TRACE_VIEW, _AGENT_VIEW],
            role,
            role,
            schema,
            role,
            schema,
            oid,
        ),
    ).fetchone()
    return row != (False,)


def _role_is_valid(connection, schema: str, role: str) -> bool:
    row = connection.execute(
        """SELECT r.oid,r.rolsuper,r.rolbypassrls,r.rolcreaterole,r.rolcreatedb,r.rolcanlogin,
                  has_schema_privilege(r.rolname,%s,'USAGE'),
                  has_schema_privilege(r.rolname,%s,'CREATE')
           FROM pg_roles r WHERE r.rolname=%s""",
        (schema, schema, role),
    ).fetchone()
    if row is None:
        return False
    oid, superuser, bypass, create_role, create_db, can_login, usage, create = row
    if superuser or bypass or create_role or create_db or not can_login or not usage or create:
        return False
    membership = connection.execute(
        "SELECT 1 FROM pg_auth_members WHERE roleid=%s OR member=%s LIMIT 1",
        (oid, oid),
    ).fetchone()
    if membership is not None:
        return False
    return not _has_unauthorized_relation_privileges(connection, schema, role, oid)


def _validate_readers(connection, schema: str, snapshot: _Snapshot, target: str | None) -> None:
    roles = [role for role, _ in snapshot.scope_rows]
    if len(roles) != len(set(roles)) or len({tenant for _, tenant in snapshot.scope_rows}) != len(
        snapshot.scope_rows
    ):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")
    for role in roles:
        if not _role_is_valid(connection, schema, role):
            raise FleetPostgresConfigurationError("fleet_reader_invalid")
    if (
        target is not None
        and target not in roles
        and not _role_is_valid(connection, schema, target)
    ):
        raise FleetPostgresConfigurationError("fleet_reader_invalid")
    if tuple(sorted(snapshot.acl_rows)) != _expected_acl(snapshot.scope_rows):
        raise FleetPostgresConfigurationError("fleet_schema_invalid")


def _target_state(snapshot: _Snapshot, role: str, tenant: str) -> str:
    mappings = [
        mapped_tenant for mapped_role, mapped_tenant in snapshot.scope_rows if mapped_role == role
    ]
    acl_views = {
        view
        for view, grantee, privilege, grantable in snapshot.acl_rows
        if grantee == role and privilege == "SELECT" and not grantable
    }
    if not mappings and not acl_views:
        return "absent"
    if mappings == [tenant] and acl_views == {_TRACE_VIEW, _AGENT_VIEW}:
        return "enabled"
    raise FleetPostgresConfigurationError("fleet_target_invalid")


def _create_shared(connection, schema: str) -> None:
    from psycopg import sql

    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                """CREATE TABLE {}.{}(
                     reader_role TEXT NOT NULL PRIMARY KEY,
                     tenant_id TEXT NOT NULL UNIQUE,
                     CHECK(octet_length(reader_role) BETWEEN 1 AND 63),
                     CHECK(tenant_id <> 'local' AND
                           tenant_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$'))"""
            ).format(sql.Identifier(schema), sql.Identifier(_SCOPE_TABLE))
        )
        cursor.execute(
            sql.SQL(
                """CREATE VIEW {}.{} WITH (security_barrier=true) AS
                   SELECT t.tenant_id,t.trace_id,t.started_at,t.ended_at,
                          (t.error IS NOT NULL) AS error_present,t.provider,
                          t.request_model,t.response_model,t.service_name,t.environment,
                          t.input_tokens,t.output_tokens,t.latency_ms,t.cost_usd,t.parent_span_id
                   FROM {}.traces AS t JOIN {}.{} AS s ON s.tenant_id=t.tenant_id
                   WHERE s.reader_role=session_user"""
            ).format(
                sql.Identifier(schema),
                sql.Identifier(_TRACE_VIEW),
                sql.Identifier(schema),
                sql.Identifier(schema),
                sql.Identifier(_SCOPE_TABLE),
            )
        )
        cursor.execute(
            sql.SQL(
                """CREATE VIEW {}.{} WITH (security_barrier=true) AS
                   SELECT e.tenant_id,e.trace_id,count(*)::bigint AS link_count,
                          CASE WHEN count(*)=1 THEN min(e.event_id) END AS agent_event_id,
                          CASE WHEN count(*)=1 THEN min(e.turn_id) END AS agent_turn_id,
                          CASE WHEN count(*)=1 THEN min(e.run_id) END AS agent_run_id
                   FROM {}.agent_events AS e
                   JOIN {}.{} AS s ON s.tenant_id=e.tenant_id
                   WHERE s.reader_role=session_user AND e.trace_id IS NOT NULL
                   GROUP BY e.tenant_id,e.trace_id"""
            ).format(
                sql.Identifier(schema),
                sql.Identifier(_AGENT_VIEW),
                sql.Identifier(schema),
                sql.Identifier(schema),
                sql.Identifier(_SCOPE_TABLE),
            )
        )


def _create_index(connection, schema: str) -> None:
    from psycopg import sql

    connection.execute(
        sql.SQL("CREATE INDEX CONCURRENTLY {} ON {}.traces(tenant_id,started_at,trace_id)").format(
            sql.Identifier(_TRACE_INDEX),
            sql.Identifier(schema),
        )
    )


def _drop_index(connection, schema: str) -> None:
    from psycopg import sql

    connection.execute(
        sql.SQL("DROP INDEX CONCURRENTLY {}.{}").format(
            sql.Identifier(schema),
            sql.Identifier(_TRACE_INDEX),
        )
    )


def _enable_target(connection, schema: str, role: str, tenant: str, add_ledger: bool) -> None:
    from psycopg import sql

    with connection.transaction(), connection.cursor() as cursor:
        if add_ledger:
            cursor.execute(
                sql.SQL("INSERT INTO {}.verdict_schema_migrations(name) VALUES (%s)").format(
                    sql.Identifier(schema)
                ),
                (_LEDGER_NAME,),
            )
        cursor.execute(
            sql.SQL("INSERT INTO {}.{}(reader_role,tenant_id) VALUES (%s,%s)").format(
                sql.Identifier(schema),
                sql.Identifier(_SCOPE_TABLE),
            ),
            (role, tenant),
        )
        for view in (_TRACE_VIEW, _AGENT_VIEW):
            cursor.execute(
                sql.SQL("GRANT SELECT ON {}.{} TO {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(view),
                    sql.Identifier(role),
                )
            )


def _disable_target(connection, schema: str, role: str) -> None:
    from psycopg import sql

    with connection.transaction(), connection.cursor() as cursor:
        for view in (_TRACE_VIEW, _AGENT_VIEW):
            cursor.execute(
                sql.SQL("REVOKE SELECT ON {}.{} FROM {}").format(
                    sql.Identifier(schema),
                    sql.Identifier(view),
                    sql.Identifier(role),
                )
            )
        cursor.execute(
            sql.SQL("DELETE FROM {}.{} WHERE reader_role=%s").format(
                sql.Identifier(schema),
                sql.Identifier(_SCOPE_TABLE),
            ),
            (role,),
        )


def _acquire_lock(connection, deadline: float) -> None:
    while time.monotonic() < deadline:
        row = connection.execute(
            """SELECT pg_try_advisory_lock(
                 hashtextextended(current_database() || ':verdict-fleet-read-v1',0))"""
        ).fetchone()
        if row == (True,):
            return
        time.sleep(0.05)
    raise FleetPostgresConfigurationError("fleet_lock_busy")


def _require_time(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise FleetPostgresConfigurationError("fleet_deadline_exceeded")


def configure_fleet(database_url: str, *, action: str, reader_role: str, tenant_id: str) -> None:
    database_url = _validate_database_url(database_url)
    reader_role = _validate_reader_role(reader_role)
    tenant_id = _tenant(tenant_id)
    if action not in {"prepare", "disable"}:
        raise ValueError("unsupported fleet action")
    try:
        import psycopg
    except ImportError as exc:
        raise ImportError(
            'Fleet ReadPort requires `pip install "cognifity-verdict[postgres]"`'
        ) from exc

    operation_deadline = time.monotonic() + _PREPARE_DEADLINE_SECONDS
    try:
        raw_connection = psycopg.connect(database_url, autocommit=True, connect_timeout=2)
    except Exception:
        raise FleetPostgresConfigurationError("fleet_database_unavailable") from None
    connection = _DeadlineConnection(raw_connection, operation_deadline)
    locked = False
    try:
        _acquire_lock(
            connection,
            min(operation_deadline, time.monotonic() + _LOCK_DEADLINE_SECONDS),
        )
        locked = True
        schema, owner = _schema_context(connection)
        _validate_base_schema(connection, schema, owner)
        snapshot = _snapshot(connection, schema, owner)
        _validate_shared(snapshot)
        if snapshot.shared_presence == (True, True, True):
            _validate_readers(
                connection,
                schema,
                snapshot,
                reader_role if action == "prepare" else None,
            )
        if action == "disable":
            if snapshot.shared_presence == (False, False, False):
                return
            state = _target_state(snapshot, reader_role, tenant_id)
            if state == "absent":
                return
            _require_time(operation_deadline)
            _disable_target(connection, schema, reader_role)
            final = _snapshot(connection, schema, owner)
            _validate_shared(final)
            _validate_readers(connection, schema, final, None)
            if _target_state(final, reader_role, tenant_id) != "absent":
                raise FleetPostgresConfigurationError("fleet_disable_failed")
            return

        if snapshot.shared_presence == (False, False, False):
            if not _role_is_valid(connection, schema, reader_role):
                raise FleetPostgresConfigurationError("fleet_reader_invalid")
            _require_time(operation_deadline)
            _create_shared(connection, schema)
            snapshot = _snapshot(connection, schema, owner)
            _validate_shared(snapshot)
            _validate_readers(connection, schema, snapshot, reader_role)
        if snapshot.index_present and not snapshot.index_valid:
            _require_time(operation_deadline)
            _drop_index(connection, schema)
            raise FleetPostgresConfigurationError("fleet_index_invalid_removed")
        if not snapshot.index_present:
            _require_time(operation_deadline)
            try:
                _create_index(connection, schema)
            except Exception:
                present, valid = _index_state(connection, schema)
                if present and not valid:
                    _drop_index(connection, schema)
                raise FleetPostgresConfigurationError("fleet_index_creation_failed") from None
        snapshot = _snapshot(connection, schema, owner)
        _validate_shared(snapshot)
        _validate_readers(connection, schema, snapshot, reader_role)
        if not snapshot.index_valid:
            raise FleetPostgresConfigurationError("fleet_index_invalid")
        state = _target_state(snapshot, reader_role, tenant_id)
        if state == "enabled":
            if snapshot.ledger_rows != (_LEDGER_NAME,):
                raise FleetPostgresConfigurationError("fleet_schema_invalid")
            return
        if any(mapped_tenant == tenant_id for _, mapped_tenant in snapshot.scope_rows):
            raise FleetPostgresConfigurationError("fleet_target_invalid")
        if len(snapshot.scope_rows) >= _MAX_READERS:
            raise FleetPostgresConfigurationError("fleet_reader_limit_exceeded")
        _require_time(operation_deadline)
        _enable_target(
            connection,
            schema,
            reader_role,
            tenant_id,
            add_ledger=not snapshot.ledger_rows,
        )
        final = _snapshot(connection, schema, owner)
        _validate_shared(final)
        _validate_readers(connection, schema, final, reader_role)
        if (
            final.ledger_rows != (_LEDGER_NAME,)
            or not final.index_valid
            or _target_state(final, reader_role, tenant_id) != "enabled"
        ):
            raise FleetPostgresConfigurationError("fleet_prepare_failed")
    finally:
        if locked:
            try:
                raw_connection.execute(
                    """SELECT pg_advisory_unlock(
                         hashtextextended(current_database() || ':verdict-fleet-read-v1',0))"""
                )
            except Exception:
                pass
        raw_connection.close()


def _scaled_integer(value: object, *, scale: Decimal, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("numeric source value is invalid")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("numeric source value is invalid")
    try:
        candidate = Decimal(str(value)) * scale
        if not candidate.is_finite() or candidate < 0:
            raise ValueError("numeric source value is invalid")
        rounded = int(candidate.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, OverflowError, ValueError):
        raise ValueError("numeric source value is invalid") from None
    if rounded > maximum:
        raise ValueError("numeric source value is invalid")
    return rounded


def _row_to_contribution(row: tuple[Any, ...], tenant_id: str) -> TraceContributionReadV1:
    if len(row) != 19:
        raise ValueError("fleet row shape is invalid")
    (
        row_tenant,
        trace_id,
        started_at,
        ended_at,
        error_present,
        provider,
        request_model,
        response_model,
        service_name,
        environment,
        input_tokens,
        output_tokens,
        latency_ms,
        cost_usd,
        parent_span_id,
        link_count,
        agent_event_id,
        agent_turn_id,
        agent_run_id,
    ) = row
    if row_tenant != tenant_id or type(error_present) is not bool:
        raise ValueError("fleet row identity is invalid")
    if link_count is None:
        link_count = 0
    if isinstance(link_count, bool) or not isinstance(link_count, int) or link_count not in {0, 1}:
        raise ValueError("fleet agent link count is invalid")
    link_state = "exact" if link_count == 1 else "not_found"
    if ended_at is None:
        status = "in_progress"
    else:
        status = "failed" if error_present else "succeeded"
    return TraceContributionReadV1(
        tenant_id=row_tenant,
        trace_id=trace_id,
        started_at=started_at,
        ended_at=ended_at,
        request_status=status,
        provider=provider,
        request_model=request_model,
        response_model=response_model,
        service_name=service_name,
        environment=environment,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_us=_scaled_integer(latency_ms, scale=Decimal("1000"), maximum=86_400_000_000),
        cost_micro_usd=_scaled_integer(cost_usd, scale=Decimal("1000000"), maximum=10**15),
        parent_span_id=parent_span_id,
        agent_link_state=link_state,
        agent_event_id=agent_event_id,
        agent_turn_id=agent_turn_id,
        agent_run_id=agent_run_id,
    )


class FleetPostgresAdapter:
    def __init__(self, database_url: str, *, tenant_id: str) -> None:
        self._database_url = _validate_database_url(database_url)
        self._tenant_id = _tenant(tenant_id)
        self._condition = threading.Condition()
        self._active = False
        self._closed = False
        self._pool_closed = False
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:
            raise ImportError(
                'Fleet ReadPort requires `pip install "cognifity-verdict[postgres]"`'
            ) from exc
        try:
            pool = ConnectionPool(
                conninfo=self._database_url,
                min_size=1,
                max_size=1,
                timeout=_POOL_ACQUIRE_SECONDS,
                max_waiting=1,
                kwargs={"autocommit": True, "connect_timeout": 2},
                open=False,
            )
            pool.open(wait=True, timeout=2)
            self._pool = pool
            self._validate_startup()
        except VerdictFleetReadError:
            if "pool" in locals():
                pool.close(timeout=2)
            raise
        except Exception:
            if "pool" in locals():
                pool.close(timeout=2)
            raise VerdictFleetReadError("read_unavailable") from None

    def _validate_startup(self) -> None:
        with self._pool.connection(timeout=_POOL_ACQUIRE_SECONDS) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(f"SET LOCAL statement_timeout='{_STATEMENT_TIMEOUT_MS}ms'")
                cursor.execute(
                    """SELECT current_setting('server_version_num')::integer,
                              session_user,current_user,current_schemas(false),
                              current_setting('transaction_read_only')::boolean,
                              current_setting('default_transaction_read_only')::boolean"""
                )
                row = cursor.fetchone()
                if row is None:
                    raise VerdictFleetReadError("read_unavailable")
                version, session_user, current_user, schemas, txn_read_only, default_read_only = row
                if not 160000 <= version < 170000:
                    raise VerdictFleetReadError("unsupported_version")
                if (
                    session_user != current_user
                    or not isinstance(schemas, list)
                    or len(schemas) != 1
                    or not txn_read_only
                    or not default_read_only
                ):
                    raise VerdictFleetReadError("read_unavailable")
                schema = schemas[0]
                relations = cursor.execute(
                    "SELECT to_regclass(%s),to_regclass(%s)",
                    (f"{schema}.{_TRACE_VIEW}", f"{schema}.{_AGENT_VIEW}"),
                ).fetchone()
                if relations is None or None in relations:
                    raise VerdictFleetReadError("unsupported_version")
                view_access = cursor.execute(
                    "SELECT has_table_privilege(session_user,%s,'SELECT'),"
                    "has_table_privilege(session_user,%s,'SELECT')",
                    relations,
                ).fetchone()
                if view_access != (True, True):
                    raise VerdictFleetReadError("read_unavailable")
                columns = cursor.execute(
                    """SELECT table_name,column_name
                       FROM information_schema.columns
                       WHERE table_schema=%s AND table_name=ANY(%s)
                       ORDER BY table_name,ordinal_position""",
                    (schema, [_TRACE_VIEW, _AGENT_VIEW]),
                ).fetchall()
                expected = [(_AGENT_VIEW, name) for name in _AGENT_VIEW_COLUMNS] + [
                    (_TRACE_VIEW, name) for name in _TRACE_VIEW_COLUMNS
                ]
                if columns != expected:
                    raise VerdictFleetReadError("unsupported_version")
                role = cursor.execute(
                    """SELECT oid,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolcanlogin
                       FROM pg_roles WHERE rolname=session_user"""
                ).fetchone()
                if role is None or role[1:] != (False, False, False, False, True):
                    raise VerdictFleetReadError("read_unavailable")
                if (
                    cursor.execute(
                        "SELECT 1 FROM pg_auth_members WHERE roleid=%s OR member=%s LIMIT 1",
                        (role[0], role[0]),
                    ).fetchone()
                    is not None
                ):
                    raise VerdictFleetReadError("read_unavailable")
                schema_privileges = cursor.execute(
                    """SELECT has_schema_privilege(session_user,%s,'USAGE'),
                              has_schema_privilege(session_user,%s,'CREATE')""",
                    (schema, schema),
                ).fetchone()
                if schema_privileges != (True, False) or _has_unauthorized_relation_privileges(
                    cursor, schema, session_user, role[0]
                ):
                    raise VerdictFleetReadError("read_unavailable")
                visible_tenants = cursor.execute(
                    f"SELECT tenant_id FROM {_TRACE_VIEW} "  # nosec B608
                    "GROUP BY tenant_id ORDER BY tenant_id LIMIT 2"
                ).fetchall()
                if visible_tenants and visible_tenants != [(self._tenant_id,)]:
                    raise VerdictFleetReadError("read_unavailable")

    def read_trace_window(
        self,
        *,
        tenant_id: str,
        window_start: datetime,
        window_end: datetime,
    ) -> TraceWindowReadV1:
        read_started_at = datetime.now(timezone.utc)
        started = time.monotonic()
        deadline = started + _READ_DEADLINE_SECONDS
        try:
            tenant = _tenant(tenant_id)
            validated_query = TraceWindowReadV1(
                schema_version=VERDICT_FLEET_READ_SCHEMA_VERSION,
                tenant_id=tenant,
                window_start=window_start,
                window_end=window_end,
                read_started_at=read_started_at,
                read_completed_at=read_started_at,
                item_count=0,
                items=(),
            )
            start_utc = validated_query.window_start
            end_utc = validated_query.window_end
        except Exception:
            raise VerdictFleetReadError("invalid_query") from None
        if tenant != self._tenant_id:
            raise VerdictFleetReadError("invalid_query")
        with self._condition:
            if self._closed or self._active:
                raise VerdictFleetReadError("read_unavailable")
            self._active = True
        rows: list[tuple[Any, ...]] = []
        failed = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            with self._pool.connection(timeout=min(_POOL_ACQUIRE_SECONDS, remaining)) as connection:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    cursor.execute(f"SET LOCAL statement_timeout='{_STATEMENT_TIMEOUT_MS}ms'")
                    cursor.execute(
                        f"""SELECT t.tenant_id,t.trace_id,t.started_at,t.ended_at,
                                   t.error_present,t.provider,t.request_model,t.response_model,
                                   t.service_name,t.environment,t.input_tokens,t.output_tokens,
                                   t.latency_ms,t.cost_usd,t.parent_span_id,
                                   a.link_count,a.agent_event_id,a.agent_turn_id,a.agent_run_id
                            FROM {_TRACE_VIEW} t
                            LEFT JOIN {_AGENT_VIEW} a
                              ON a.tenant_id=t.tenant_id AND a.trace_id=t.trace_id
                            WHERE t.tenant_id=%s AND t.started_at >= %s AND t.started_at < %s
                            ORDER BY t.started_at,t.trace_id LIMIT 2001""",  # nosec B608
                        (tenant, start_utc, end_utc),
                    )
                    rows = cursor.fetchall()
        except Exception:
            failed = True
        finally:
            with self._condition:
                self._active = False
                self._condition.notify_all()
        if failed:
            raise VerdictFleetReadError("read_unavailable")
        if time.monotonic() > deadline:
            raise VerdictFleetReadError("read_unavailable")
        if len(rows) > _MAX_ITEMS:
            raise VerdictFleetReadError("window_too_dense")
        invalid_model = False
        try:
            items = tuple(_row_to_contribution(row, tenant) for row in rows)
            completed_at = datetime.now(timezone.utc)
            result = TraceWindowReadV1(
                schema_version=VERDICT_FLEET_READ_SCHEMA_VERSION,
                tenant_id=tenant,
                window_start=start_utc,
                window_end=end_utc,
                read_started_at=read_started_at,
                read_completed_at=completed_at,
                item_count=len(items),
                items=items,
            )
            trace_window_read_to_json(result)
        except VerdictFleetReadError:
            raise
        except Exception:
            invalid_model = True
            result = None
        if invalid_model:
            raise VerdictFleetReadError("invalid_read_model")
        with self._condition:
            late_or_closed = time.monotonic() > deadline or self._closed
        if late_or_closed or result is None:
            raise VerdictFleetReadError("read_unavailable")
        return result

    def close(self) -> None:
        deadline = time.monotonic() + _READ_DEADLINE_SECONDS
        with self._condition:
            self._closed = True
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            close_pool = not self._pool_closed
            self._pool_closed = True
        if not close_pool:
            return
        failed = False
        try:
            self._pool.close(timeout=max(0.0, deadline - time.monotonic()))
        except Exception:
            failed = True
        if failed:
            raise VerdictFleetReadError("read_unavailable")
