"""Every SQL statement the read path runs, in one file.

Deliberately centralised: a reviewer — or an on-call engineer chasing a slow query
— can audit the service's entire database footprint by reading one module, rather
than grepping SQL out of route handlers.

Two rules hold throughout:

1. **All filtering, aggregation, sorting and pagination happen in SQL.** Python
   never receives a row it intends to discard, and never sums a column. The only
   per-row Python is response shaping of an already-paginated result.

2. **User input is never interpolated into SQL.** Values are bound parameters, sent
   to MySQL down a separate channel from the query text, so a value can never be
   parsed as code no matter what it contains. The two places where SQL *structure*
   varies with input — ORDER BY and GROUP BY — take identifiers, which cannot be
   bound. Those resolve through whitelist dicts instead, so user input is used to
   *look up* SQL we wrote, never to *become* SQL.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Row, text

# ---------------------------------------------------------------------------
# Discrepancy rules — the single source of truth
#
# Each is a predicate over ONE `transactions` row: no joins, no aggregation, no
# scanning event history. That is the payoff for storing milestone facts as
# nullable columns — "is broken" becomes expressible as a WHERE clause.
#
# Defining them once here means the list endpoint, the per-transaction detail view,
# the counts rollup and GET /reconciliation/rules can never disagree about what
# "broken" means.
# ---------------------------------------------------------------------------

DISCREPANCY_RULES: dict[str, str] = {
    # Payment succeeded, money never moved, and we are past the settlement SLA.
    # The SLA test is what makes this time-relative — and therefore why
    # discrepancies are computed at read time rather than flagged at ingest.
    "processed_never_settled": (
        "(t.processed_at IS NOT NULL AND t.first_settled_at IS NULL "
        " AND t.failed_at IS NULL AND t.processed_at < :cutoff)"
    ),
    # Money moved for a payment that explicitly failed. Direct financial loss.
    # This single line is the whole "settlement recorded for a failed payment"
    # requirement — and it is only expressible because payment_status and
    # settlement_status are independent axes. One combined status column would
    # have overwritten 'failed' with 'settled' and erased the evidence.
    "settled_despite_failure": "(t.failed_at IS NOT NULL AND t.first_settled_at IS NOT NULL)",
    # Settled more than once by genuinely DISTINCT events. Note this counts events,
    # not rows: a settlement replayed under the same event_id never reaches the
    # counter, because INSERT IGNORE drops it before the recompute runs. In the
    # sample data that distinction is the difference between 162 and 95.
    "duplicate_settlement": "(t.settled_event_count > 1)",
    # Initiated, then nothing, ever. Not a payment/settlement contradiction as such,
    # but an unresolved transaction past SLA is something ops must chase.
    "stuck_pending": (
        "(t.initiated_at IS NOT NULL AND t.processed_at IS NULL "
        " AND t.failed_at IS NULL AND t.first_settled_at IS NULL "
        " AND t.initiated_at < :cutoff)"
    ),
    # Settlement with no preceding processing. Impossible if upstream is honest and
    # complete — which is exactly why it earns a rule. It is also the precise
    # corruption an order-dependent state machine would manufacture from
    # out-of-order events, so a non-zero count here means distrust the ingest path,
    # not the merchant. Expected: 0.
    "settled_without_processing": (
        "(t.first_settled_at IS NOT NULL AND t.processed_at IS NULL AND t.failed_at IS NULL)"
    ),
}

# Every rule a row breaches, as a comma-separated string.
#
# Postgres would use array_remove(ARRAY[...], NULL). MySQL has no array type, but
# CONCAT_WS skips NULL arguments — so non-matching CASE arms vanish and a clean row
# yields ''. Python splits it back into a list at the response boundary.
_DISCREPANCY_LIST_SQL = "CONCAT_WS(',', {arms})".format(
    arms=", ".join(
        f"CASE WHEN {pred} THEN '{name}' END" for name, pred in DISCREPANCY_RULES.items()
    )
)

_ANY_DISCREPANCY_SQL = "(" + " OR ".join(DISCREPANCY_RULES.values()) + ")"

# Human-readable explanation per rule. Presentation only — formatting a page of
# <=100 already-selected rows is Python's job; selecting them was SQL's.
DISCREPANCY_EXPLANATIONS: dict[str, str] = {
    "processed_never_settled": "Payment was processed but has not settled within the SLA window.",
    "settled_despite_failure": "Settlement was recorded for a payment that failed.",
    "duplicate_settlement": "Transaction was settled more than once by distinct events.",
    "stuck_pending": "Payment was initiated but never processed, failed or settled.",
    "settled_without_processing": (
        "Settlement was recorded without any preceding payment processing."
    ),
}

# ---------------------------------------------------------------------------
# Whitelists for the parts of the SQL that vary with request input
# ---------------------------------------------------------------------------

SORTABLE_COLUMNS: dict[str, str] = {
    "initiated_at": "t.initiated_at",
    "processed_at": "t.processed_at",
    "settled_at": "t.first_settled_at",
    "last_event_at": "t.last_event_at",
    "amount": "t.amount",
}

SUMMARY_DIMENSIONS: dict[str, str] = {
    # `date` is the UTC calendar date of payment_initiated — the transaction's
    # business date. A transaction we only ever saw a settlement for has no
    # initiated_at and groups under NULL, which is the honest answer.
    "merchant": "t.merchant_id",
    "date": "DATE(t.initiated_at)",
    "status": "t.status",
    "payment_status": "t.payment_status",
    "settlement_status": "t.settlement_status",
}

_TXN_COLUMNS = """
    t.transaction_id, t.merchant_id, m.merchant_name, t.amount, t.currency,
    t.status, t.payment_status, t.settlement_status,
    t.initiated_at, t.processed_at, t.failed_at,
    t.first_settled_at, t.last_settled_at,
    t.event_count, t.settled_event_count, t.last_event_at
"""


@dataclass
class TransactionFilters:
    merchant_id: str | None = None
    status: str | None = None
    payment_status: str | None = None
    settlement_status: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None


def _build_filters(f: TransactionFilters) -> tuple[list[str], dict[str, Any]]:
    """Turn filters into SQL conditions plus bound params. Values are never inlined."""
    conditions: list[str] = []
    params: dict[str, Any] = {}

    if f.merchant_id:
        conditions.append("t.merchant_id = :merchant_id")
        params["merchant_id"] = f.merchant_id
    if f.status:
        conditions.append("t.status = :status")
        params["status"] = f.status
    if f.payment_status:
        conditions.append("t.payment_status = :payment_status")
        params["payment_status"] = f.payment_status
    if f.settlement_status:
        conditions.append("t.settlement_status = :settlement_status")
        params["settlement_status"] = f.settlement_status
    # Date filters run against initiated_at so the range scan can use
    # idx_txn_initiated / idx_txn_merchant_initiated. date_to is EXCLUSIVE, which
    # avoids the classic BETWEEN bug of silently dropping everything timestamped
    # after 00:00:00.000 on the final day.
    if f.date_from:
        conditions.append("t.initiated_at >= :date_from")
        params["date_from"] = f.date_from
    if f.date_to:
        conditions.append("t.initiated_at < :date_to")
        params["date_to"] = f.date_to

    return conditions, params


# ---------------------------------------------------------------------------
# GET /transactions
# ---------------------------------------------------------------------------


def list_transactions(
    conn: Connection,
    filters: TransactionFilters,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
) -> tuple[list[Row], int]:
    """One page of transactions plus the total matching count.

    `COUNT(*) OVER ()` (a window function, MySQL 8+) returns the full count
    alongside the page in a single round trip — the window is evaluated after WHERE
    but before LIMIT, so it sees all 3,800 while we return 50. The alternative — a
    second COUNT query — doubles the round trips AND can disagree with the page
    under concurrent writes, since the two would run in different snapshots.
    """
    conditions, params = _build_filters(filters)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Whitelist lookup: an unknown key raises long before reaching SQL.
    order_col = SORTABLE_COLUMNS[sort_by]
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"

    # transaction_id is an unconditional tie-breaker, and it is load-bearing.
    # SQL guarantees ordering BETWEEN different sort values but says nothing about
    # ties — the engine may permute tied rows differently on each query. Under
    # OFFSET pagination that shows one row on two pages and silently skips another.
    # Adding a unique column makes the ordering total, so every row has exactly one
    # position. One clause; the difference between correct and almost-correct.
    #
    # MySQL sorts NULLs first ASC / last DESC natively, which is already what an
    # operator expects (un-reached milestones at the bottom when sorting by "most
    # recent"), so no explicit NULLS clause is needed — MySQL doesn't support one.
    sql = text(f"""
        SELECT {_TXN_COLUMNS}, COUNT(*) OVER () AS total_count
        FROM transactions t
        JOIN merchants m ON m.merchant_id = t.merchant_id
        {where}
        ORDER BY {order_col} {direction}, t.transaction_id {direction}
        LIMIT :limit OFFSET :offset
    """)
    rows = conn.execute(sql, {**params, "limit": limit, "offset": offset}).fetchall()
    total = rows[0].total_count if rows else _count_transactions(conn, filters)
    return list(rows), total


def _count_transactions(conn: Connection, filters: TransactionFilters) -> int:
    """Count for the empty-page case, where COUNT(*) OVER () has no row to ride on."""
    conditions, params = _build_filters(filters)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return conn.execute(text(f"SELECT COUNT(*) FROM transactions t {where}"), params).scalar_one()


# ---------------------------------------------------------------------------
# GET /transactions/{id}
# ---------------------------------------------------------------------------


def get_transaction(conn: Connection, transaction_id: str, cutoff: datetime) -> Row | None:
    sql = text(f"""
        SELECT {_TXN_COLUMNS},
               {_DISCREPANCY_LIST_SQL} AS discrepancies
        FROM transactions t
        JOIN merchants m ON m.merchant_id = t.merchant_id
        WHERE t.transaction_id = :transaction_id
    """)
    return conn.execute(sql, {"transaction_id": transaction_id, "cutoff": cutoff}).fetchone()


def get_transaction_events(conn: Connection, transaction_id: str) -> list[Row]:
    """Full event history, oldest first — including duplicates and superseded events.

    This is the audit trail. It is what lets an operator answer *why* a transaction
    is in a given state, which is the entire reason payment_events is append-only.

    The ordering is served directly by idx_events_txn_time (transaction_id,
    occurred_at): index order IS output order, so there is no sort node. event_id
    breaks ties between two events sharing a timestamp.
    """
    sql = text("""
        SELECT event_id, event_type, amount, currency, occurred_at, received_at
        FROM payment_events
        WHERE transaction_id = :transaction_id
        ORDER BY occurred_at ASC, event_id ASC
    """)
    return list(conn.execute(sql, {"transaction_id": transaction_id}).fetchall())


# ---------------------------------------------------------------------------
# GET /reconciliation/summary
# ---------------------------------------------------------------------------


def reconciliation_summary(
    conn: Connection,
    group_by: list[str],
    filters: TransactionFilters,
) -> list[Row]:
    """Aggregate the projection across caller-chosen dimensions.

    One GROUP BY serves every combination of dimensions rather than a hand-written
    query per shape (three times the code, three places to drift apart). The CASE
    aggregates give conditional totals in a single pass; the alternative — one
    subquery per metric — scans the table once per metric.

    The value reconciliation is the substance:
        expected_settlement_amount  money that SHOULD have settled (processed)
        settled_amount              money that actually settled
        unreconciled_amount         the gap ops has to chase

    unreconciled_amount goes NEGATIVE when money settled that shouldn't have — a
    settled-despite-failure transaction counts toward settled_amount but never
    toward expected. Negative here is a real signal, not a bug. Do not ABS() it.
    """
    dims = [SUMMARY_DIMENSIONS[d] for d in group_by]  # whitelist; raises on unknown

    select_parts: list[str] = []
    for name, expr in zip(group_by, dims, strict=True):
        if name == "merchant":
            select_parts.append(f"{expr} AS merchant_id")
            select_parts.append("m.merchant_name AS merchant_name")
        else:
            select_parts.append(f"{expr} AS `{name}`")

    group_parts = list(dims)
    if "merchant" in group_by:
        # merchant_name is functionally dependent on merchant_id (its PK), but it
        # comes from the joined table so MySQL needs it named explicitly under
        # ONLY_FULL_GROUP_BY (the default since 5.7).
        group_parts.append("m.merchant_name")

    conditions, params = _build_filters(filters)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    select_clause = (",\n               ".join(select_parts) + ",") if select_parts else ""
    group_clause = f"GROUP BY {', '.join(group_parts)}" if group_parts else ""
    order_clause = f"ORDER BY {', '.join(dims)}" if dims else ""

    # COALESCE(..., 0) everywhere: a group with no processed payments would
    # otherwise report NULL instead of 0, and NULL in a financial report is a
    # support ticket.
    sql = text(f"""
        SELECT {select_clause}
               COUNT(*) AS transaction_count,
               COALESCE(SUM(t.amount), 0) AS total_amount,

               COUNT(CASE WHEN t.payment_status = 'processed' THEN 1 END) AS processed_count,
               COUNT(CASE WHEN t.payment_status = 'failed'    THEN 1 END) AS failed_count,
               COUNT(CASE WHEN t.payment_status = 'pending'   THEN 1 END) AS pending_count,
               COUNT(CASE WHEN t.settlement_status = 'settled'   THEN 1 END) AS settled_count,
               COUNT(CASE WHEN t.settlement_status = 'unsettled' THEN 1 END) AS unsettled_count,

               COALESCE(SUM(CASE WHEN t.payment_status = 'processed' THEN t.amount END), 0)
                   AS expected_settlement_amount,
               COALESCE(SUM(CASE WHEN t.settlement_status = 'settled' THEN t.amount END), 0)
                   AS settled_amount,
               COALESCE(SUM(CASE WHEN t.payment_status = 'processed' THEN t.amount END), 0)
             - COALESCE(SUM(CASE WHEN t.settlement_status = 'settled' THEN t.amount END), 0)
                   AS unreconciled_amount
        FROM transactions t
        JOIN merchants m ON m.merchant_id = t.merchant_id
        {where}
        {group_clause}
        {order_clause}
    """)
    return list(conn.execute(sql, params).fetchall())


# ---------------------------------------------------------------------------
# GET /reconciliation/discrepancies
# ---------------------------------------------------------------------------


def discrepancy_counts(conn: Connection, cutoff: datetime, filters: TransactionFilters) -> dict:
    """Count and value per discrepancy type, in ONE pass over the table.

    Postgres would `unnest` the computed array and GROUP BY it. MySQL has no arrays
    and no unnest, so instead we emit one conditional COUNT/SUM pair per rule —
    still a single scan. The alternative (five COUNT queries UNION ALL'd) reads the
    table five times to learn the same thing.

    Returns {rule_name: {"count": n, "total_amount": x}}. A transaction breaching
    two rules is counted under both, so these intentionally sum to more than the
    number of distinct broken rows.
    """
    metrics = ",\n               ".join(
        f"COUNT(CASE WHEN {pred} THEN 1 END) AS `{name}__count`,\n"
        f"               COALESCE(SUM(CASE WHEN {pred} THEN t.amount END), 0) AS `{name}__amount`"
        for name, pred in DISCREPANCY_RULES.items()
    )
    conditions, params = _build_filters(filters)
    conditions.append(_ANY_DISCREPANCY_SQL)
    where = f"WHERE {' AND '.join(conditions)}"

    row = conn.execute(
        text(f"SELECT {metrics} FROM transactions t {where}"), {**params, "cutoff": cutoff}
    ).one()
    m = row._mapping
    return {
        name: {"count": m[f"{name}__count"], "total_amount": m[f"{name}__amount"]}
        for name in DISCREPANCY_RULES
    }


def list_discrepancies(
    conn: Connection,
    as_of: datetime,
    cutoff: datetime,
    filters: TransactionFilters,
    discrepancy_type: str | None,
    limit: int,
    offset: int,
) -> tuple[list[Row], int]:
    """Transactions whose payment and settlement state disagree.

    The WHERE clause is an explicit OR of the raw rule predicates rather than a test
    on the computed CONCAT_WS string. That is deliberate: a test like
    `discrepancies <> ''` is opaque to the optimiser and forces a full scan, whereas
    the OR'd predicates are indexable expressions the optimiser can reason about.
    Same answer, different plan.
    """
    # Whitelist: only a known rule name can select a predicate.
    predicate = DISCREPANCY_RULES[discrepancy_type] if discrepancy_type else _ANY_DISCREPANCY_SQL

    conditions, params = _build_filters(filters)
    conditions.append(predicate)
    where = f"WHERE {' AND '.join(conditions)}"

    sql = text(f"""
        SELECT {_TXN_COLUMNS},
               {_DISCREPANCY_LIST_SQL} AS discrepancies,
               -- Age from as_of, NOT from the SLA cutoff: an operator triaging this
               -- asks "how long has this been broken?", and that answer must not
               -- shift when they tune sla_hours.
               TIMESTAMPDIFF(SECOND,
                   COALESCE(t.processed_at, t.initiated_at, t.first_settled_at),
                   :as_of) / 3600.0 AS age_hours,
               COUNT(*) OVER () AS total_count
        FROM transactions t
        JOIN merchants m ON m.merchant_id = t.merchant_id
        {where}
        ORDER BY t.initiated_at DESC, t.transaction_id DESC
        LIMIT :limit OFFSET :offset
    """)
    rows = conn.execute(
        sql, {**params, "as_of": as_of, "cutoff": cutoff, "limit": limit, "offset": offset}
    ).fetchall()

    if rows:
        return list(rows), rows[0].total_count

    total = conn.execute(
        text(f"SELECT COUNT(*) FROM transactions t {where}"), {**params, "cutoff": cutoff}
    ).scalar_one()
    return [], total


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def health_stats(conn: Connection) -> dict[str, Any]:
    """Cheap readiness signal that also proves the DB is reachable AND populated.

    Returning row counts means a reviewer hitting the deployed URL can confirm at a
    glance that the sample data is loaded, rather than finding an empty database
    behind a healthy-looking service.
    """
    row = conn.execute(
        text("""
            SELECT (SELECT COUNT(*) FROM payment_events) AS events,
                   (SELECT COUNT(*) FROM transactions)   AS transactions,
                   (SELECT COUNT(*) FROM merchants)      AS merchants
        """)
    ).one()
    return {"events": row.events, "transactions": row.transactions, "merchants": row.merchants}
