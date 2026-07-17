"""GET /transactions and GET /transactions/{id}."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import Connection, Row

from app.config import Settings, get_settings
from app.db import get_connection
from app.deps import CutoffDep
from app.queries import (
    SORTABLE_COLUMNS,
    TransactionFilters,
    get_transaction,
    get_transaction_events,
    list_transactions,
    split_discrepancies,
)
from app.schemas import (
    EventOut,
    MerchantOut,
    PageMeta,
    PaymentStatus,
    SettlementStatus,
    TransactionDetailOut,
    TransactionListOut,
    TransactionOut,
    TransactionStatus,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])

SortBy = Annotated[
    str,
    Query(description=f"Sort field. One of: {', '.join(SORTABLE_COLUMNS)}"),
]


def _to_transaction(row: Row) -> TransactionOut:
    return TransactionOut(
        transaction_id=row.transaction_id,
        merchant=MerchantOut(merchant_id=row.merchant_id, merchant_name=row.merchant_name),
        amount=row.amount,
        currency=row.currency,
        status=row.status,
        payment_status=row.payment_status,
        settlement_status=row.settlement_status,
        initiated_at=row.initiated_at,
        processed_at=row.processed_at,
        failed_at=row.failed_at,
        first_settled_at=row.first_settled_at,
        last_settled_at=row.last_settled_at,
        event_count=row.event_count,
        settled_event_count=row.settled_event_count,
        last_event_at=row.last_event_at,
    )


@router.get(
    "",
    response_model=TransactionListOut,
    summary="List transactions with filtering, sorting and pagination",
)
def get_transactions(
    conn: Annotated[Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    merchant_id: Annotated[str | None, Query(description="Exact merchant id.")] = None,
    status_: Annotated[
        TransactionStatus | None,
        Query(
            alias="status",
            description="Flattened status: settled > failed > processed > pending.",
        ),
    ] = None,
    payment_status: Annotated[PaymentStatus | None, Query()] = None,
    settlement_status: Annotated[SettlementStatus | None, Query()] = None,
    date_from: Annotated[
        datetime | None, Query(description="Inclusive lower bound on initiated_at (ISO 8601).")
    ] = None,
    date_to: Annotated[
        datetime | None, Query(description="Exclusive upper bound on initiated_at (ISO 8601).")
    ] = None,
    sort_by: SortBy = "initiated_at",
    sort_dir: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TransactionListOut:
    """List transactions.

    `status` is the flattened convenience axis. `payment_status` and
    `settlement_status` are the two real axes and can be combined to express
    states the flattened one cannot — e.g. `payment_status=failed&
    settlement_status=settled` returns exactly the transactions where money moved
    for a failed payment.

    Filtering, sorting, counting and pagination are all done by the database; this
    handler only shapes the page it is handed.
    """
    if sort_by not in SORTABLE_COLUMNS:
        # Explicit over a bare KeyError: tell the caller what is allowed. Sort
        # fields are whitelisted because ORDER BY takes an identifier, and
        # identifiers cannot be bound as parameters — building this clause from
        # raw input would be an injection vector.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid sort_by '{sort_by}'. Allowed: {sorted(SORTABLE_COLUMNS)}",
        )
    if date_from and date_to and date_from >= date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="date_from must be strictly before date_to.",
        )

    limit = min(limit, settings.max_page_size)
    filters = TransactionFilters(
        merchant_id=merchant_id,
        status=status_.value if status_ else None,
        payment_status=payment_status.value if payment_status else None,
        settlement_status=settlement_status.value if settlement_status else None,
        date_from=date_from,
        date_to=date_to,
    )
    rows, total = list_transactions(conn, filters, sort_by, sort_dir, limit, offset)

    return TransactionListOut(
        pagination=PageMeta(
            total=total,
            limit=limit,
            offset=offset,
            returned=len(rows),
            has_more=offset + len(rows) < total,
        ),
        data=[_to_transaction(r) for r in rows],
    )


@router.get(
    "/{transaction_id}",
    response_model=TransactionDetailOut,
    summary="Fetch one transaction with its merchant and full event history",
    responses={404: {"description": "Transaction not found"}},
)
def get_transaction_detail(
    conn: Annotated[Connection, Depends(get_connection)],
    cutoff: CutoffDep,
    transaction_id: Annotated[UUID, Path(description="Transaction UUID.")],
) -> TransactionDetailOut:
    """Fetch a single transaction.

    Returns current state, merchant, any discrepancies, and the complete event
    history oldest-first — including the events that were superseded or that
    duplicated others. That history is the audit trail: it is what lets an
    operator answer *why* a transaction is in this state, which is the whole
    reason the event log is append-only.
    """
    _, _, cutoff_ts = cutoff
    row = get_transaction(conn, str(transaction_id), cutoff_ts)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found.",
        )

    events = get_transaction_events(conn, str(transaction_id))
    base = _to_transaction(row)
    return TransactionDetailOut(
        **base.model_dump(),
        discrepancies=split_discrepancies(row.discrepancies),
        events=[
            EventOut(
                event_id=e.event_id,
                event_type=e.event_type,
                amount=e.amount,
                currency=e.currency,
                occurred_at=e.occurred_at,
                received_at=e.received_at,
            )
            for e in events
        ],
    )
