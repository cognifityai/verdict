"""Live PostgreSQL tests for the opt-in Fleet ReadPort V1 boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

import pytest
from _postgres_test_safety import isolated_test_dsn, validate_test_dsn
from verdict.fleet_read_port import (
    PostgresVerdictFleetReadPortV1,
    VerdictFleetReadError,
    trace_window_read_to_json,
)
from verdict.storage.postgres import PostgresStorage

DSN, POSTGRES_SKIP_REASON = validate_test_dsn(
    os.environ.get("VERDICT_TEST_POSTGRES_DSN"),
    allow_any_database=os.environ.get("VERDICT_TEST_POSTGRES_ALLOW_ANY_DB") == "1",
)
if os.environ.get("VERDICT_REQUIRE_POSTGRES_TESTS") == "1" and DSN is None:
    raise RuntimeError(
        "VERDICT_REQUIRE_POSTGRES_TESTS=1 but live PostgreSQL tests are unsafe: "
        f"{POSTGRES_SKIP_REASON}"
    )

pytestmark = [
    pytest.mark.skipif(DSN is None, reason=POSTGRES_SKIP_REASON),
    pytest.mark.filterwarnings("error::DeprecationWarning:psycopg_pool.*"),
]


def _run_fleet_cli(
    action: str, *, owner_dsn: str, role: str, tenant_id: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "verdict.fleet_read_port",
            action,
            "--reader-role",
            role,
            "--tenant-id",
            tenant_id,
        ],
        env={**os.environ, "VERDICT_DATABASE_URL": owner_dsn},
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


@contextmanager
def _fleet_database(*, tenant_id: str = "tenant-a"):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    role = f"verdict_fleet_reader_{uuid4().hex[:16]}"
    password = f"fleet-{uuid4().hex}!:@"
    with isolated_test_dsn(DSN) as owner_dsn:
        storage = PostgresStorage(owner_dsn, min_pool=1, max_pool=1)
        storage.close()
        owner_parts = conninfo_to_dict(owner_dsn)
        database = owner_parts["dbname"]
        schema = owner_parts["options"].removeprefix("-csearch_path=")

        def postgres_url(*, user: str, password: str | None, options: str) -> str:
            credential = quote(user, safe="")
            if password is not None:
                credential += f":{quote(password, safe='')}"
            host = owner_parts.get("host", "localhost")
            port = owner_parts.get("port", "5432")
            return (
                f"postgresql://{credential}@{host}:{port}/{quote(database, safe='')}?"
                f"options={quote(options, safe='')}"
            )

        owner_url = postgres_url(
            user=owner_parts["user"],
            password=owner_parts.get("password"),
            options=f"-csearch_path={schema}",
        )
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            admin.execute(
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(
                    sql.Identifier(role)
                )
            )
            admin.execute(
                sql.SQL("ALTER ROLE {} IN DATABASE {} SET search_path={}").format(
                    sql.Identifier(role),
                    sql.Identifier(database),
                    sql.Identifier(schema),
                )
            )
        reader_dsn = postgres_url(
            user=role,
            password=password,
            options=f"-csearch_path={schema} -cdefault_transaction_read_only=on",
        )
        try:
            yield owner_url, reader_dsn, role, schema, tenant_id
        finally:
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE usename=%s AND pid<>pg_backend_pid()",
                    (role,),
                )
                admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _insert_trace_and_agent_link(
    owner_dsn: str,
    *,
    tenant_id: str,
    trace_id: str,
    include_agent_link: bool = True,
    ended_at: datetime | None = datetime(2026, 9, 14, 12, 0, 1, 250000, tzinfo=timezone.utc),
    error: str | None = None,
    latency_ms: float | None = 1.2345,
) -> None:
    import psycopg

    started_at = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    suffix = trace_id.replace(":", "-")
    with psycopg.connect(owner_dsn, autocommit=True) as owner:
        owner.execute(
            """INSERT INTO traces(
                   trace_id,started_at,ended_at,provider,request_model,response_model,
                   input_tokens,output_tokens,error,latency_ms,prompt_redacted,
                   response_redacted,raw_messages,tenant_id,tags,cost_usd,
                   parent_span_id,service_name,environment)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                trace_id,
                started_at,
                ended_at,
                "openai",
                "request-model",
                "response-model",
                17,
                23,
                error,
                latency_ms,
                "private-prompt-canary",
                "private-response-canary",
                json.dumps([{"content": "private-raw-canary"}]),
                tenant_id,
                json.dumps({"private-tag-canary": "must-not-cross"}),
                0.0005005,
                "parent-span",
                "checkout",
                "preview",
            ),
        )
        if not include_agent_link:
            return
        owner.execute(
            """INSERT INTO import_sources(
                   tenant_id,source_session_id,source_kind,source_locator_hash,
                   started_at,observed_at)
               VALUES (%s,%s,'test',%s,%s,%s)""",
            (tenant_id, f"session-{suffix}", "a" * 64, started_at, started_at),
        )
        owner.execute(
            """INSERT INTO agent_runs(
                   tenant_id,run_id,source_session_id,started_at,status)
               VALUES (%s,%s,%s,%s,'completed')""",
            (tenant_id, f"run-{suffix}", f"session-{suffix}", started_at),
        )
        owner.execute(
            """INSERT INTO agent_turns(
                   tenant_id,run_id,turn_id,sequence,started_at,status,
                   request_state,response_state)
               VALUES (%s,%s,%s,0,%s,'completed',
                       'not_captured','not_captured')""",
            (tenant_id, f"run-{suffix}", f"turn-{suffix}", started_at),
        )
        owner.execute(
            """INSERT INTO agent_events(
                   tenant_id,event_id,run_id,turn_id,sequence,occurred_at,event_type,
                   status,provenance,attributes_json,privacy_classification,trace_id)
               VALUES (%s,%s,%s,%s,0,%s,'model_call','completed',
                       'test','{}','metadata',%s)""",
            (
                tenant_id,
                f"event-{suffix}",
                f"run-{suffix}",
                f"turn-{suffix}",
                started_at,
                trace_id,
            ),
        )


def test_live_fleet_prepare_read_isolation_and_disable() -> None:
    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        _insert_trace_and_agent_link(owner_dsn, tenant_id=tenant_id, trace_id="trace-a")
        _insert_trace_and_agent_link(owner_dsn, tenant_id="tenant-b", trace_id="trace-b")

        first_prepare = _run_fleet_cli(
            "prepare", owner_dsn=owner_dsn, role=role, tenant_id=tenant_id
        )
        repeat_prepare = _run_fleet_cli(
            "prepare", owner_dsn=owner_dsn, role=role, tenant_id=tenant_id
        )
        assert (first_prepare.returncode, first_prepare.stdout, first_prepare.stderr) == (0, "", "")
        assert (repeat_prepare.returncode, repeat_prepare.stdout, repeat_prepare.stderr) == (
            0,
            "",
            "",
        )

        import psycopg

        with psycopg.connect(reader_dsn) as direct_reader:
            direct_reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            startup_state = direct_reader.execute(
                """SELECT session_user,current_user,current_schemas(false),
                          current_setting('transaction_read_only')::boolean,
                          current_setting('default_transaction_read_only')::boolean"""
            ).fetchone()
        assert startup_state == (role, role, [_schema], True, True)
        with psycopg.connect(reader_dsn) as direct_reader:
            boundary = direct_reader.execute(
                """SELECT has_schema_privilege(session_user,current_schema(),'USAGE'),
                          has_schema_privilege(session_user,current_schema(),'CREATE'),
                          has_table_privilege(session_user,'traces','SELECT'),
                          has_table_privilege(session_user,'agent_runs','SELECT'),
                          has_table_privilege(
                              session_user,'verdict_fleet_reader_scope_v1','SELECT')"""
            ).fetchone()
            visible = direct_reader.execute(
                "SELECT DISTINCT tenant_id FROM verdict_fleet_traces_v1 ORDER BY tenant_id"
            ).fetchall()
            grants = direct_reader.execute(
                """SELECT table_name,privilege_type,is_grantable
                   FROM information_schema.role_table_grants
                   WHERE grantee=session_user AND table_schema=current_schema()
                   ORDER BY table_name,privilege_type"""
            ).fetchall()
        assert boundary == (True, False, False, False, False)
        assert visible == [(tenant_id,)]
        assert grants == [
            ("verdict_fleet_agent_links_v1", "SELECT", "NO"),
            ("verdict_fleet_traces_v1", "SELECT", "NO"),
        ]

        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        result = reader.read_trace_window(
            tenant_id=tenant_id,
            window_start=now,
            window_end=now + timedelta(minutes=15),
        )
        encoded = trace_window_read_to_json(result)

        assert result.item_count == 1
        assert result.items[0].trace_id == "trace-a"
        assert result.items[0].latency_us == 1_234
        assert result.items[0].cost_micro_usd == 500
        assert result.items[0].agent_link_state == "exact"
        assert result.items[0].agent_event_id == "event-trace-a"
        assert result.items[0].agent_turn_id == "turn-trace-a"
        assert result.items[0].agent_run_id == "run-trace-a"
        assert "trace-b" not in encoded
        assert "private-prompt-canary" not in encoded
        assert "private-response-canary" not in encoded
        assert "private-raw-canary" not in encoded
        assert "private-tag-canary" not in encoded

        changed_prepare = _run_fleet_cli(
            "prepare", owner_dsn=owner_dsn, role=role, tenant_id="tenant-b"
        )
        changed_disable = _run_fleet_cli(
            "disable", owner_dsn=owner_dsn, role=role, tenant_id="tenant-b"
        )
        assert (changed_prepare.returncode, changed_prepare.stdout, changed_prepare.stderr) == (
            1,
            "",
            "fleet prepare failed\n",
        )
        assert (changed_disable.returncode, changed_disable.stdout, changed_disable.stderr) == (
            1,
            "",
            "fleet disable failed\n",
        )

        with pytest.raises(VerdictFleetReadError, match="invalid_query"):
            reader.read_trace_window(
                tenant_id="tenant-b",
                window_start=now,
                window_end=now + timedelta(minutes=15),
            )
        with pytest.raises(VerdictFleetReadError, match="invalid_query"):
            reader.read_trace_window(
                tenant_id=tenant_id,
                window_start=now.replace(tzinfo=None),
                window_end=(now + timedelta(minutes=15)).replace(tzinfo=None),
            )
        reader.close()
        reader.close()
        with pytest.raises(VerdictFleetReadError, match="read_unavailable"):
            reader.read_trace_window(
                tenant_id=tenant_id,
                window_start=now,
                window_end=now + timedelta(minutes=15),
            )

        first_disable = _run_fleet_cli(
            "disable", owner_dsn=owner_dsn, role=role, tenant_id=tenant_id
        )
        repeat_disable = _run_fleet_cli(
            "disable", owner_dsn=owner_dsn, role=role, tenant_id=tenant_id
        )
        assert (first_disable.returncode, first_disable.stdout, first_disable.stderr) == (0, "", "")
        assert (repeat_disable.returncode, repeat_disable.stdout, repeat_disable.stderr) == (
            0,
            "",
            "",
        )
        with pytest.raises(VerdictFleetReadError, match="read_unavailable"):
            PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)


@pytest.mark.parametrize("grant_kind", ["table", "column"])
def test_live_prepare_rejects_reader_with_existing_base_table_access_without_mutation(
    grant_kind: str,
) -> None:
    import psycopg
    from psycopg import sql
    from verdict._fleet_postgres import FleetPostgresConfigurationError, configure_fleet

    with _fleet_database() as (owner_dsn, _reader_dsn, role, schema, tenant_id):
        with psycopg.connect(owner_dsn, autocommit=True) as owner:
            if grant_kind == "table":
                grant = sql.SQL("GRANT SELECT ON {}.judgments TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            else:
                grant = sql.SQL("GRANT SELECT(prompt_redacted) ON {}.traces TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                )
            owner.execute(grant)

        with pytest.raises(FleetPostgresConfigurationError, match="fleet_reader_invalid"):
            configure_fleet(
                owner_dsn,
                action="prepare",
                reader_role=role,
                tenant_id=tenant_id,
            )

        with psycopg.connect(owner_dsn) as owner:
            presence = owner.execute(
                """SELECT to_regclass('verdict_fleet_reader_scope_v1'),
                          to_regclass('verdict_fleet_traces_v1'),
                          to_regclass('verdict_fleet_agent_links_v1')"""
            ).fetchone()
        assert presence == (None, None, None)


@pytest.mark.parametrize("reader_is_member", [True, False])
def test_live_prepare_rejects_role_membership_in_either_direction(
    reader_is_member: bool,
) -> None:
    import psycopg
    from psycopg import sql
    from verdict._fleet_postgres import FleetPostgresConfigurationError, configure_fleet

    companion = f"verdict_fleet_companion_{uuid4().hex[:16]}"
    with _fleet_database() as (owner_dsn, _reader_dsn, role, _schema, tenant_id):
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(companion)))
            granted = (companion, role) if reader_is_member else (role, companion)
            admin.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(granted[0]), sql.Identifier(granted[1])
                )
            )
        try:
            with pytest.raises(FleetPostgresConfigurationError, match="fleet_reader_invalid"):
                configure_fleet(
                    owner_dsn,
                    action="prepare",
                    reader_role=role,
                    tenant_id=tenant_id,
                )
        finally:
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(companion)))


@pytest.mark.parametrize(
    ("tamper", "action"),
    [
        ("ledger", "prepare"),
        ("view_option", "prepare"),
        ("partial_grant", "disable"),
        ("extra_grant", "prepare"),
        ("column_grant", "prepare"),
        ("index_definition", "prepare"),
    ],
)
def test_live_operator_fails_closed_on_partial_or_unknown_prepared_state(
    tamper: str,
    action: str,
) -> None:
    import psycopg
    from psycopg import sql
    from verdict._fleet_postgres import FleetPostgresConfigurationError, configure_fleet

    with _fleet_database() as (owner_dsn, _reader_dsn, role, _schema, tenant_id):
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        with psycopg.connect(owner_dsn, autocommit=True) as owner:
            if tamper == "ledger":
                owner.execute(
                    "DELETE FROM verdict_schema_migrations "
                    "WHERE name LIKE 'verdict_fleet_read_v1:%'"
                )
            elif tamper == "view_option":
                owner.execute("ALTER VIEW verdict_fleet_traces_v1 SET (security_barrier=false)")
            elif tamper == "partial_grant":
                owner.execute(
                    sql.SQL("REVOKE SELECT ON verdict_fleet_agent_links_v1 FROM {}").format(
                        sql.Identifier(role)
                    )
                )
            elif tamper == "extra_grant":
                owner.execute(
                    sql.SQL("GRANT INSERT ON verdict_fleet_traces_v1 TO {}").format(
                        sql.Identifier(role)
                    )
                )
            elif tamper == "column_grant":
                owner.execute(
                    sql.SQL("GRANT SELECT(trace_id) ON verdict_fleet_traces_v1 TO {}").format(
                        sql.Identifier(role)
                    )
                )
            else:
                owner.execute("DROP INDEX verdict_fleet_traces_tenant_started_trace_v1")
                owner.execute(
                    """CREATE INDEX verdict_fleet_traces_tenant_started_trace_v1
                       ON traces(tenant_id DESC,started_at,trace_id)"""
                )

        with pytest.raises(FleetPostgresConfigurationError):
            configure_fleet(
                owner_dsn,
                action=action,
                reader_role=role,
                tenant_id=tenant_id,
            )


def test_live_prepare_caps_reader_mappings_at_eight() -> None:
    import psycopg
    from psycopg import sql
    from verdict._fleet_postgres import FleetPostgresConfigurationError, configure_fleet

    extra_roles = [f"verdict_fleet_extra_{uuid4().hex[:16]}" for _ in range(8)]
    with _fleet_database() as (owner_dsn, _reader_dsn, role, schema, tenant_id):
        with psycopg.connect(DSN, autocommit=True) as admin:
            for extra_role in extra_roles:
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(extra_role)))
                admin.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema), sql.Identifier(extra_role)
                    )
                )
                admin.execute(
                    sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
                        sql.Identifier(schema), sql.Identifier(extra_role)
                    )
                )
        try:
            configure_fleet(
                owner_dsn,
                action="prepare",
                reader_role=role,
                tenant_id=tenant_id,
            )
            for position, extra_role in enumerate(extra_roles[:7], start=1):
                configure_fleet(
                    owner_dsn,
                    action="prepare",
                    reader_role=extra_role,
                    tenant_id=f"tenant-{position}",
                )
            with pytest.raises(
                FleetPostgresConfigurationError, match="fleet_reader_limit_exceeded"
            ):
                configure_fleet(
                    owner_dsn,
                    action="prepare",
                    reader_role=extra_roles[7],
                    tenant_id="tenant-8",
                )
            with psycopg.connect(owner_dsn) as owner:
                mappings = owner.execute(
                    "SELECT reader_role,tenant_id FROM verdict_fleet_reader_scope_v1"
                ).fetchall()
            assert len(mappings) == 8
            assert extra_roles[7] not in {mapped_role for mapped_role, _ in mappings}
        finally:
            with psycopg.connect(DSN, autocommit=True) as admin:
                for extra_role in reversed(extra_roles):
                    admin.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(extra_role)))
                    admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(extra_role)))


def test_live_prepare_bounds_busy_advisory_lock_without_mutation() -> None:
    import psycopg
    from verdict._fleet_postgres import FleetPostgresConfigurationError, configure_fleet

    with _fleet_database() as (owner_dsn, _reader_dsn, role, _schema, tenant_id):
        lock_owner = psycopg.connect(owner_dsn, autocommit=True)
        lock_owner.execute(
            """SELECT pg_advisory_lock(
                 hashtextextended(current_database() || ':verdict-fleet-read-v1',0))"""
        )
        started = time.monotonic()
        try:
            with pytest.raises(FleetPostgresConfigurationError, match="fleet_lock_busy"):
                configure_fleet(
                    owner_dsn,
                    action="prepare",
                    reader_role=role,
                    tenant_id=tenant_id,
                )
        finally:
            lock_owner.close()
        elapsed = time.monotonic() - started

        assert 1.8 <= elapsed < 3
        with psycopg.connect(owner_dsn) as owner:
            presence = owner.execute(
                """SELECT to_regclass('verdict_fleet_reader_scope_v1'),
                          to_regclass('verdict_fleet_traces_v1'),
                          to_regclass('verdict_fleet_agent_links_v1')"""
            ).fetchone()
        assert presence == (None, None, None)


def test_live_read_derives_status_and_not_found_without_error_text() -> None:
    from verdict._fleet_postgres import configure_fleet

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        _insert_trace_and_agent_link(
            owner_dsn,
            tenant_id=tenant_id,
            trace_id="in-progress",
            include_agent_link=False,
            ended_at=None,
        )
        _insert_trace_and_agent_link(
            owner_dsn,
            tenant_id=tenant_id,
            trace_id="failed",
            include_agent_link=False,
            error="private-error-canary",
        )
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        try:
            result = reader.read_trace_window(
                tenant_id=tenant_id,
                window_start=now,
                window_end=now + timedelta(minutes=15),
            )
        finally:
            reader.close()

        assert [(item.trace_id, item.request_status) for item in result.items] == [
            ("failed", "failed"),
            ("in-progress", "in_progress"),
        ]
        assert all(item.agent_link_state == "not_found" for item in result.items)
        assert all(item.agent_event_id is None for item in result.items)
        assert "private-error-canary" not in trace_window_read_to_json(result)


def test_live_read_rejects_malformed_numeric_source_row() -> None:
    from verdict._fleet_postgres import configure_fleet

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        _insert_trace_and_agent_link(
            owner_dsn,
            tenant_id=tenant_id,
            trace_id="malformed",
            include_agent_link=False,
            latency_ms=-1,
        )
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        try:
            with pytest.raises(VerdictFleetReadError) as raised:
                reader.read_trace_window(
                    tenant_id=tenant_id,
                    window_start=now,
                    window_end=now + timedelta(minutes=15),
                )
        finally:
            reader.close()

        assert raised.value.code == "invalid_read_model"
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None


def test_live_read_rejects_dense_window_without_truncating() -> None:
    import psycopg
    from verdict._fleet_postgres import configure_fleet

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        with psycopg.connect(owner_dsn, autocommit=True) as owner:
            owner.execute(
                """INSERT INTO traces(trace_id,tenant_id,started_at,ended_at)
                   SELECT 'dense-' || value::text,%s,%s + value * interval '1 microsecond',
                          %s + interval '1 second'
                   FROM generate_series(1,2001) AS value""",
                (tenant_id, now, now),
            )
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        try:
            with pytest.raises(VerdictFleetReadError, match="window_too_dense"):
                reader.read_trace_window(
                    tenant_id=tenant_id,
                    window_start=now,
                    window_end=now + timedelta(minutes=15),
                )
        finally:
            reader.close()


def test_live_read_rejects_serialized_response_over_two_mib() -> None:
    import psycopg
    from verdict._fleet_postgres import configure_fleet

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        with psycopg.connect(owner_dsn, autocommit=True) as owner:
            owner.execute(
                """INSERT INTO traces(
                       trace_id,tenant_id,started_at,ended_at,provider,request_model,
                       response_model,service_name,environment,parent_span_id)
                   SELECT lpad(value::text,256,'t'),%s,
                          %s + value * interval '1 microsecond',%s + interval '1 second',
                          repeat('p',128),repeat('q',256),repeat('r',256),
                          repeat('s',128),repeat('e',128),repeat('a',256)
                   FROM generate_series(1,2000) AS value""",
                (tenant_id, now, now),
            )
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        try:
            with pytest.raises(VerdictFleetReadError, match="response_limit_exceeded"):
                reader.read_trace_window(
                    tenant_id=tenant_id,
                    window_start=now,
                    window_end=now + timedelta(minutes=15),
                )
        finally:
            reader.close()


def test_live_close_bounds_active_read_and_discards_its_late_result() -> None:
    import psycopg
    from verdict._fleet_postgres import configure_fleet

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        _insert_trace_and_agent_link(
            owner_dsn,
            tenant_id=tenant_id,
            trace_id="blocked",
            include_agent_link=False,
        )
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        blocker = psycopg.connect(owner_dsn)
        blocker.execute("LOCK TABLE traces IN ACCESS EXCLUSIVE MODE")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                reader.read_trace_window,
                tenant_id=tenant_id,
                window_start=now,
                window_end=now + timedelta(minutes=15),
            )
            active_deadline = time.monotonic() + 1
            while time.monotonic() < active_deadline:
                with reader._adapter._condition:
                    if reader._adapter._active:
                        break
                time.sleep(0.005)
            else:
                pytest.fail("fleet read did not become active")

            close_started = time.monotonic()
            reader.close()
            close_elapsed = time.monotonic() - close_started
            with pytest.raises(VerdictFleetReadError, match="read_unavailable") as raised:
                pending.result(timeout=0.5)
        blocker.rollback()
        blocker.close()

        assert close_elapsed < 2.1
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        with pytest.raises(VerdictFleetReadError, match="read_unavailable"):
            reader.read_trace_window(
                tenant_id=tenant_id,
                window_start=now,
                window_end=now + timedelta(minutes=15),
            )


def test_live_read_rejects_ambiguous_reverse_agent_link() -> None:
    import psycopg
    from verdict._fleet_postgres import configure_fleet

    now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    with _fleet_database() as (owner_dsn, reader_dsn, role, _schema, tenant_id):
        _insert_trace_and_agent_link(owner_dsn, tenant_id=tenant_id, trace_id="ambiguous")
        configure_fleet(
            owner_dsn,
            action="prepare",
            reader_role=role,
            tenant_id=tenant_id,
        )
        with psycopg.connect(owner_dsn, autocommit=True) as owner:
            owner.execute("DROP INDEX idx_agent_events_trace")
            owner.execute(
                """INSERT INTO import_sources(
                       tenant_id,source_session_id,source_kind,source_locator_hash,
                       started_at,observed_at)
                   VALUES (%s,'session-second','test',%s,%s,%s)""",
                (tenant_id, "b" * 64, now, now),
            )
            owner.execute(
                """INSERT INTO agent_runs(
                       tenant_id,run_id,source_session_id,started_at,status)
                   VALUES (%s,'run-second','session-second',%s,'completed')""",
                (tenant_id, now),
            )
            owner.execute(
                """INSERT INTO agent_turns(
                       tenant_id,run_id,turn_id,sequence,started_at,status,
                       request_state,response_state)
                   VALUES (%s,'run-second','turn-second',0,%s,'completed',
                           'not_captured','not_captured')""",
                (tenant_id, now),
            )
            owner.execute(
                """INSERT INTO agent_events(
                       tenant_id,event_id,run_id,turn_id,sequence,occurred_at,event_type,
                       status,provenance,attributes_json,privacy_classification,trace_id)
                   VALUES (%s,'event-second','run-second','turn-second',0,%s,
                           'model_call','completed','test','{}','metadata','ambiguous')""",
                (tenant_id, now),
            )

        reader = PostgresVerdictFleetReadPortV1(reader_dsn, tenant_id=tenant_id)
        try:
            with pytest.raises(VerdictFleetReadError, match="invalid_read_model"):
                reader.read_trace_window(
                    tenant_id=tenant_id,
                    window_start=now,
                    window_end=now + timedelta(minutes=15),
                )
        finally:
            reader.close()
