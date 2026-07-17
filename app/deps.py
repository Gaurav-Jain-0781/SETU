"""Shared request-scoped dependencies."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Query

from app.config import Settings, get_settings


def resolve_cutoff(
    settings: Annotated[Settings, Depends(get_settings)],
    as_of: Annotated[
        datetime | None,
        Query(description="Evaluate reconciliation as of this instant (default: now, UTC)."),
    ] = None,
    sla_hours: Annotated[
        int | None,
        Query(ge=0, le=8760, description="Settlement SLA in hours (default: 24)."),
    ] = None,
) -> tuple[datetime, int, datetime]:
    """Resolve (as_of, sla_hours, cutoff) for time-relative discrepancy rules.

    Exposing `as_of` rather than hardcoding now() buys two things:

    * Ops teams reconcile against a fixed point — "as of yesterday 23:59" — so a
      report can be re-run later and produce the same numbers. Anchoring to
      wall-clock makes yesterday's report unreproducible.
    * Tests become deterministic. A rule that says "older than 24h" is otherwise
      untestable without either sleeping or freezing the clock.

    `cutoff` is the instant before which an unresolved transaction is late.
    """
    now = as_of.astimezone(UTC) if as_of else datetime.now(UTC)
    hours = sla_hours if sla_hours is not None else settings.settlement_sla_hours
    return now, hours, now - timedelta(hours=hours)


CutoffDep = Annotated[tuple[datetime, int, datetime], Depends(resolve_cutoff)]
