import pytest
from verdict.dashboard.storage_url import is_postgres_storage


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("postgresql://localhost/verdict", True),
        ("host=localhost dbname=verdict", True),
        ("host=localhost password='https://secret.invalid'", True),
        ("password=secret", True),
        ("dbname=verdict.db", True),
        ("sslcert=/tmp/client.crt", True),
        ("client_encoding=UTF8", True),
        ("sqlite:////tmp/verdict.db", False),
        ("/tmp/verdict=a15.db", False),
        ("relative-verdict.db", False),
        ("unknown=value", False),
    ],
)
def test_dashboard_storage_classification(value, expected):
    assert is_postgres_storage(value) is expected
