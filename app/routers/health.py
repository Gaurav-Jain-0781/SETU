"""Liveness and readiness."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import Connection

from app.db import get_connection
from app.queries import health_stats

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    """Process is up. Deliberately does not touch the database.

    A liveness probe that queries Postgres would report the *app* as dead during
    a transient DB blip and invite the orchestrator to restart a process that is
    fine. Liveness answers "is this process wedged?"; readiness answers "can it
    serve traffic?". Conflating them causes restart loops during DB failover.
    """
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe with row counts")
def ready(conn: Annotated[Connection, Depends(get_connection)]) -> dict[str, object]:
    """Database is reachable and the schema is present.

    Returns row counts so a reviewer hitting the deployed URL can confirm at a
    glance that the sample data is actually loaded, rather than finding an empty
    database behind a healthy-looking service.
    """
    return {"status": "ready", "counts": health_stats(conn)}
