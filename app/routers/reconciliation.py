"""GET /reconciliation/summary and GET /reconciliation/discrepancies."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Connection, Row

from app.config import Settings, get_settings
from app.db import get_connection
from app.deps import CutoffDep
from app.queries import (
    DISCREPANCY_EXPLANATIONS,
    DISCREPANCY_RULES,
    SUMMARY_DIMENSIONS,
    TransactionFilters,
    discrepancy_counts,
    list_discrepancies,
    reconciliation_summary,
    split_discrepancies,
)
from app.schemas import (
    DiscrepancyCount,
    DiscrepancyListOut,
    DiscrepancyOut,
    DiscrepancyType,
    MerchantOut,
    PageMeta,
    SummaryGroup,
    SummaryOut,
    SummaryTotals,
)

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])

_ZERO = Decimal("0")


@router.get(
    "/summary",
    response_model=SummaryOut,
    summary="Reconciliation summary grouped by chosen dimensions",
)
def get_summary(
    conn: Annotated[Connection, Depends(get_connection)],
    group_by: Annotated[
        str,
        Query(
            description=(
                "Comma-separated dimensions, applied in order. "
                f"Allowed: {', '.join(SUMMARY_DIMENSIONS)}. "
                "Pass an empty value for grand totals only."
            ),
            examples=["merchant", "merchant,date", "date,status"],
        ),
    ] = "merchant",
    merchant_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
) -> SummaryOut:
    """Summarise transactions across any combination of dimensions.

    Beyond counts, this reports **value reconciliation** per group:

    | field | meaning |
    |---|---|
    | `expected_settlement_amount` | money that should have settled (processed payments) |
    | `settled_amount` | money that actually settled |
    | `unreconciled_amount` | `expected - settled` — the gap to chase |

    `unreconciled_amount` can be negative, and that is meaningful rather than a
    bug: a settlement recorded against a failed payment adds to `settled_amount`
    but never to `expected`, so money moving that shouldn't have shows up as a
    negative gap.

    Every number here is computed by one `GROUP BY` with `FILTER` aggregates —
    a single pass over the rows, no per-metric subqueries and no Python
    arithmetic.
    """
    dimensions = [d.strip() for d in group_by.split(",") if d.strip()]
    unknown = [d for d in dimensions if d not in SUMMARY_DIMENSIONS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown group_by dimension(s): {unknown}. Allowed: {sorted(SUMMARY_DIMENSIONS)}",
        )
    if len(set(dimensions)) != len(dimensions):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Duplicate dimensions in group_by.",
        )
    if date_from and date_to and date_from >= date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be strictly before date_to.",
        )

    filters = TransactionFilters(merchant_id=merchant_id, date_from=date_from, date_to=date_to)
    rows = reconciliation_summary(conn, dimensions, filters)

    groups = [_to_group(r, dimensions) for r in rows]
    # Grand totals are summed from the group rows rather than re-queried. This is
    # exact, not an approximation — SUM over a partition of the same rows is the
    # SUM over all of them — and it avoids a second aggregate scan. The groups are
    # a page-free complete result set, so there is nothing missing from the sum.
    totals = SummaryTotals(
        transaction_count=sum(g.transaction_count for g in groups),
        total_amount=sum((g.total_amount for g in groups), start=_ZERO),
        expected_settlement_amount=sum((g.expected_settlement_amount for g in groups), start=_ZERO),
        settled_amount=sum((g.settled_amount for g in groups), start=_ZERO),
        unreconciled_amount=sum((g.unreconciled_amount for g in groups), start=_ZERO),
    )

    return SummaryOut(
        group_by=dimensions,
        filters={
            "merchant_id": merchant_id,
            "date_from": date_from.isoformat() if date_from else None,
            "date_to": date_to.isoformat() if date_to else None,
        },
        totals=totals,
        groups=groups,
    )


@router.get(
    "/discrepancies",
    response_model=DiscrepancyListOut,
    summary="Transactions where payment state and settlement state disagree",
)
def get_discrepancies(
    conn: Annotated[Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    cutoff: CutoffDep,
    type_: Annotated[
        DiscrepancyType | None,
        Query(alias="type", description="Return only this discrepancy class."),
    ] = None,
    merchant_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DiscrepancyListOut:
    """Find transactions whose payment and settlement state are inconsistent.

    Five classes are detected:

    | type | meaning |
    |---|---|
    | `processed_never_settled` | processed, still unsettled past the SLA |
    | `settled_despite_failure` | money moved for a payment that failed |
    | `duplicate_settlement` | settled more than once, by *distinct* events |
    | `stuck_pending` | initiated and never resolved, past the SLA |
    | `settled_without_processing` | settled with no preceding processing |

    Two classes are time-relative (`processed_never_settled`, `stuck_pending`):
    a transaction becomes a discrepancy purely by getting old, with no event to
    trigger a write. That is why this is computed at query time rather than
    flagged at ingest — a stored flag would be stale the moment the clock moved.
    Use `as_of` and `sla_hours` to control the window.

    `duplicate_settlement` counts distinct settlement *events*. A settlement
    replayed under the same `event_id` is absorbed by idempotent ingestion and is
    correctly **not** reported here — in the bundled sample data that distinction
    is the difference between 162 apparent duplicates and 95 real ones.

    A transaction can breach several rules, so `counts_by_type` may sum to more
    than `pagination.total`.
    """
    as_of, sla_hours, cutoff_ts = cutoff
    if date_from and date_to and date_from >= date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be strictly before date_to.",
        )

    limit = min(limit, settings.max_page_size)
    filters = TransactionFilters(merchant_id=merchant_id, date_from=date_from, date_to=date_to)

    rows, total = list_discrepancies(
        conn, as_of, cutoff_ts, filters, type_.value if type_ else None, limit, offset
    )
    counts = discrepancy_counts(conn, cutoff_ts, filters)

    return DiscrepancyListOut(
        as_of=as_of,
        sla_hours=sla_hours,
        pagination=PageMeta(
            total=total,
            limit=limit,
            offset=offset,
            returned=len(rows),
            has_more=offset + len(rows) < total,
        ),
        # Rules that matched nothing are omitted rather than reported as zero —
        # `settled_without_processing: 0` on every response is noise. Its absence
        # is the good news; GET /reconciliation/rules is where you see the full
        # list of what we look for.
        counts_by_type=sorted(
            (
                DiscrepancyCount(type=name, count=c["count"], total_amount=c["total_amount"])
                for name, c in counts.items()
                if c["count"] > 0
            ),
            key=lambda c: c.count,
            reverse=True,
        ),
        data=[_to_discrepancy(r) for r in rows],
    )


@router.get("/rules", tags=["reconciliation"], summary="The discrepancy rules, as SQL")
def get_rules() -> dict[str, dict[str, str]]:
    """Expose the exact predicate behind each discrepancy class.

    Reconciliation output is only trustworthy if an operator can see what the
    service means by "broken". Serving the live SQL — the same strings the
    queries are built from, not a prose paraphrase that could drift — makes the
    classification auditable rather than something to take on faith.
    """
    return {
        name: {"predicate": " ".join(pred.split()), "description": DISCREPANCY_EXPLANATIONS[name]}
        for name, pred in DISCREPANCY_RULES.items()
    }


# ---------------------------------------------------------------------------
# Row -> response shaping
# ---------------------------------------------------------------------------


def _to_group(row: Row, dimensions: list[str]) -> SummaryGroup:
    m = row._mapping
    return SummaryGroup(
        merchant_id=m.get("merchant_id"),
        merchant_name=m.get("merchant_name"),
        date=str(m["date"]) if m.get("date") else None,
        status=m.get("status"),
        payment_status=m.get("payment_status"),
        settlement_status=m.get("settlement_status"),
        transaction_count=row.transaction_count,
        total_amount=row.total_amount,
        processed_count=row.processed_count,
        failed_count=row.failed_count,
        pending_count=row.pending_count,
        settled_count=row.settled_count,
        unsettled_count=row.unsettled_count,
        expected_settlement_amount=row.expected_settlement_amount,
        settled_amount=row.settled_amount,
        unreconciled_amount=row.unreconciled_amount,
    )


def _to_discrepancy(row: Row) -> DiscrepancyOut:
    kinds = split_discrepancies(row.discrepancies)
    return DiscrepancyOut(
        transaction_id=row.transaction_id,
        merchant=MerchantOut(merchant_id=row.merchant_id, merchant_name=row.merchant_name),
        amount=row.amount,
        currency=row.currency,
        payment_status=row.payment_status,
        settlement_status=row.settlement_status,
        discrepancies=kinds,
        detail=" ".join(DISCREPANCY_EXPLANATIONS[k] for k in kinds),
        initiated_at=row.initiated_at,
        processed_at=row.processed_at,
        failed_at=row.failed_at,
        first_settled_at=row.first_settled_at,
        settled_event_count=row.settled_event_count,
        age_hours=(round(float(row.age_hours), 2) if row.age_hours is not None else None),
    )
