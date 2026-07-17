"""Shared test fixtures.

Tests run against a REAL MySQL — the same engine as production — in a separate
`setu_test` database. Never SQLite: half of what this service relies on
(generated columns, INSERT IGNORE semantics, CHECK enforcement, window functions,
DECIMAL behaviour) is MySQL-specific. A test suite that passes on SQLite would be
testing a different program than the one we ship.

The `setu_test` database is created and torn down here, so running the suite can
never touch the dev data in `setu`.
"""

import os

import pytest
from sqlalchemy import create_engine, text

# Must be set BEFORE app.config is imported, since get_settings() is lru_cached
# and would otherwise bake in the dev database URL on first read.
_ADMIN_URL = os.getenv("TEST_ADMIN_URL", "mysql+pymysql://root:setu@localhost:3307")
_TEST_DB = "setu_test"
_TEST_URL = f"{_ADMIN_URL}/{_TEST_DB}"
os.environ["DATABASE_URL"] = _TEST_URL
os.environ.setdefault("DB_SSL", "false")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import apply_schema, get_engine, reset_engine  # noqa: E402
from app.main import app  # noqa: E402

_TABLES = ("payment_events", "transactions", "merchants")


@pytest.fixture(scope="session", autouse=True)
def _database():
    """Create the test database once, apply the real schema, drop it at the end.

    Uses sql/schema.sql — the same DDL production runs. Tests that ran against a
    hand-maintained test schema would silently stop testing the real one the first
    time the two drifted.
    """
    admin = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
        conn.execute(text(f"CREATE DATABASE {_TEST_DB}"))
    admin.dispose()

    reset_engine()
    apply_schema()
    yield

    reset_engine()
    admin = create_engine(_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {_TEST_DB}"))
    admin.dispose()


@pytest.fixture(autouse=True)
def _clean():
    """Empty every table before each test, so tests can't leak into each other."""
    with get_engine().begin() as conn:
        # TRUNCATE is blocked by the FKs pointing at merchants; MySQL has no
        # CASCADE for this, so drop the checks for the duration. Session-scoped —
        # nothing outside this connection loses its guarantees.
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        for table in _TABLES:
            conn.execute(text(f"TRUNCATE TABLE {table}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    yield


@pytest.fixture
def client():
    """HTTP client against the real app.

    Tests go through the API rather than calling functions directly wherever
    possible: that exercises validation, routing, serialisation and SQL together.
    A unit test of ingest_events() would prove the function works while the
    endpoint 500s.
    """
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    """Raw connection for asserting on database state directly."""
    with get_engine().connect() as conn:
        yield conn
