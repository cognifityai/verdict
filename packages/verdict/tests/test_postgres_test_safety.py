from _postgres_test_safety import validate_test_dsn


def test_live_postgres_guard_accepts_test_databases_in_both_dsn_forms():
    for dsn in (
        "postgresql://user:password@localhost/verdict_test",
        "host=localhost dbname=verdict_test user=user password=password",
    ):
        assert validate_test_dsn(dsn, allow_any_database=False) == (dsn, "")


def test_live_postgres_guard_rejects_non_test_database_without_override():
    dsn = "postgresql://user:password@localhost/verdict"

    selected, reason = validate_test_dsn(dsn, allow_any_database=False)

    assert selected is None
    assert "containing 'test'" in reason
    assert validate_test_dsn(dsn, allow_any_database=True) == (dsn, "")


def test_live_postgres_guard_rejects_invalid_dsn_without_exposing_it():
    selected, reason = validate_test_dsn(
        "password=private invalid", allow_any_database=False
    )

    assert selected is None
    assert reason == "VERDICT_TEST_POSTGRES_DSN is not a valid PostgreSQL DSN"
    assert "private" not in reason
