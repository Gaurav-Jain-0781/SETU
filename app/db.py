"""Database engine, pooling, and the request-scoped connection dependency."""

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import Engine

from app.config import get_settings

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

_engine: Engine | None = None


def get_engine() -> Engine:
    """Process-wide engine. Created lazily so importing the app never touches the DB."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            # TLS when talking to a managed provider, plain for local Docker.
            # See app/config.Settings.connect_args.
            connect_args=settings.connect_args,
            # MySQL closes idle connections after `wait_timeout` (8h by default,
            # and managed hosts often set it far lower). Without pre_ping the pool
            # hands out a connection the server already hung up on, and the first
            # query after a quiet period dies with "MySQL server has gone away".
            # One cheap round trip to never debug that.
            pool_pre_ping=True,
            # Recycle below any plausible server-side idle timeout, so we close
            # connections before the server does.
            pool_recycle=3600,
            future=True,
        )
    return _engine


def _split_statements(ddl: str) -> list[str]:
    """Split a DDL file into individual statements.

    PyMySQL refuses multiple statements in one execute() unless the
    MULTI_STATEMENTS client flag is set — and we deliberately don't set it. That
    flag would apply to *every* query on the connection, turning any future SQL
    injection from "read one table" into "run arbitrary statements". We bind all
    parameters, so injection shouldn't be possible anyway; leaving the flag off
    means a mistake stays survivable. Splitting here costs nothing.

    Assumes `--` comments occupy their own lines and no statement contains a
    semicolon inside a string literal — both true of sql/schema.sql, and enforced
    by keeping that file the only thing this parses.
    """
    lines = [ln for ln in ddl.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


def apply_schema() -> None:
    """Apply sql/schema.sql. Idempotent — every statement is CREATE ... IF NOT EXISTS.

    Running this on boot means a fresh container or a fresh deploy provisions
    itself with no manual migrate step to forget. See README § Tradeoffs for why
    this isn't Alembic.
    """
    statements = _split_statements(_SCHEMA_PATH.read_text())
    with get_engine().begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def get_connection() -> Iterator[Connection]:
    """FastAPI dependency yielding a connection inside a transaction.

    The `begin()` block COMMITs when the handler returns and ROLLBACKs if it
    raises. Ingestion correctness depends on this: the INSERT IGNORE of the events
    and the recompute of the projection must land atomically. A crash between them
    would otherwise leave the log holding events the projection never saw — the
    half-state that InnoDB's atomicity guarantee exists to make unobservable.
    """
    with get_engine().connect() as conn:
        with conn.begin():
            yield conn


def reset_engine() -> None:
    """Dispose the engine. Used by tests to rebind to a different database."""
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None
