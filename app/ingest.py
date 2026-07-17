"""Idempotent event ingestion — the core of the service.

The problem
-----------
An event arrives. We must (1) store it, (2) not store it twice, and (3) update the
transaction's row — but NOT if the event was a duplicate. Step 3 is the trap: a
replayed `settled` that bumps a counter invents a settlement that never happened.
In the bundled sample data that bug turns 95 real duplicate-settlements into 162.

Why application-level checking cannot work
-----------------------------------------
    if not event_exists(event_id):   # two concurrent replays both pass this
        insert(event)                # ...and both land here

The check and the write are separate steps and the world can change between them.
No amount of care closes that window. A PRIMARY KEY does, because it makes checking
and writing the same operation — the database decides, atomically. That is what
`INSERT IGNORE` leans on.

Why we never ask "was this event new?"
--------------------------------------
PostgreSQL answers that with RETURNING. MySQL has no RETURNING. So we sidestep the
question entirely:

    1. INSERT IGNORE the events. Duplicates are silently dropped.
       The log is now correct — the primary key guarantees it.
    2. RECOMPUTE each touched transaction by reading ALL of its events back out
       of the log.

Step 2 doesn't care whether the events were new. It reads the deduplicated log and
derives the answer; fresh or replayed, the result is identical.

    settled_event_count = COUNT(settled rows in the log)   -- recount: can't be wrong
    settled_event_count = settled_event_count + 1          -- tally: wrong on replay

A recount doesn't add to anything. It overwrites with a fresh answer. Run it once
or fifty times, same result.

Everything else falls out for free:

* **Out-of-order events self-heal.** A `settled` arriving before its
  `payment_processed` just sets first_settled_at; the next recompute sees both.
  No state machine, no "illegal transition" to reject.
* **Unknown transactions are fine.** A `settled` for a transaction we've never seen
  creates the row rather than 404-ing. Across three uncoordinated upstream systems
  you cannot assume the initiator's events arrive first.
* **The projection is rebuildable.** Drop `transactions` entirely and re-run the
  recompute without its WHERE clause. A design that increments counters can never
  do this — the increments are gone; once wrong, wrong forever.
"""

import json
from collections.abc import Sequence

from sqlalchemy import Connection, bindparam, text

from app.schemas import EventIn, EventResult, IngestOutcome, IngestResponse

# ---------------------------------------------------------------------------
# 1. Merchants must exist before payment_events can reference them (FK).
#
# INSERT IGNORE would silently skip a merchant whose name changed, so we use
# ON DUPLICATE KEY UPDATE — last write wins, meaning a rename propagates.
#
# The IF() guard suppresses no-op writes: without it, re-ingesting 10,000 events
# would rewrite all 5 merchant rows 10,000 times, churning dead rows and bumping
# updated_at for nothing. With it, we only actually write when the name changed.
# ---------------------------------------------------------------------------
_UPSERT_MERCHANT = text("""
    INSERT INTO merchants (merchant_id, merchant_name)
    VALUES (:merchant_id, :merchant_name)
    ON DUPLICATE KEY UPDATE
        merchant_name = IF(merchant_name <> VALUES(merchant_name),
                           VALUES(merchant_name), merchant_name)
""")

# ---------------------------------------------------------------------------
# 2. The idempotency boundary.
#
# INSERT IGNORE: on a PRIMARY KEY collision, silently skip the row. That is the
# entire duplicate defence, and it is enforced by the database rather than by code
# that could race or forget.
#
# Executed with executemany, so the whole batch is one round trip.
# ---------------------------------------------------------------------------
_INSERT_EVENTS = text("""
    INSERT IGNORE INTO payment_events (
        event_id, transaction_id, merchant_id, event_type,
        amount, currency, occurred_at, payload
    ) VALUES (
        :event_id, :transaction_id, :merchant_id, :event_type,
        :amount, :currency, :occurred_at, :payload
    )
""")

# Pre-existing event_ids, read BEFORE the insert, purely to label the response.
#
# This is NOT how idempotency is enforced — INSERT IGNORE does that, and would do
# it correctly even if this query returned nonsense. This only decides whether we
# report "ingested" or "duplicate" back to the caller.
#
# Under concurrent identical batches this label can be wrong (both callers read
# "not present", both get told "ingested", only one row actually lands). The DATA
# stays correct regardless — only the cosmetic label races. Accepting that keeps
# ingestion to one round trip instead of one per event; the alternative buys an
# exact label at 1,000x the round trips, for a field nobody makes decisions on.
_EXISTING_EVENT_IDS = text("""
    SELECT event_id FROM payment_events WHERE event_id IN :event_ids
""").bindparams(bindparam("event_ids", expanding=True))

# ---------------------------------------------------------------------------
# 3. The recompute. This is the whole pivot: many event rows -> one transaction row.
#
# Read it in English: "group this transaction's events. initiated_at is the
# earliest payment_initiated among them; failed_at is the earliest payment_failed —
# and if there are none, MIN over zero rows is NULL, which is exactly the 'never
# happened' the discrepancy rules need. Count the settlements. Write the row; if it
# exists, overwrite it."
#
# MySQL has no FILTER (WHERE ...), so conditional aggregates use the CASE trick:
# CASE turns non-matching rows into NULL, and MIN/COUNT ignore NULL. Same result as
# Postgres's FILTER, noisier.
#
# GROUP BY guarantees one proposed row per transaction_id, which is what makes the
# ON DUPLICATE KEY UPDATE below well-defined.
#
# merchant_id/amount/currency use MIN() as a deterministic pick: every event of a
# transaction agrees on these (verified across all 10,355 sample events), so any
# aggregate returns the same value. They are invariants of the transaction, not
# merge targets. See README § Assumptions.
# ---------------------------------------------------------------------------
_RECOMPUTE_TEMPLATE = """
    INSERT INTO transactions (
        transaction_id, merchant_id, amount, currency,
        initiated_at, processed_at, failed_at, first_settled_at, last_settled_at,
        settled_event_count, event_count, first_event_at, last_event_at
    )
    SELECT
        e.transaction_id,
        MIN(e.merchant_id),
        MIN(e.amount),
        MIN(e.currency),
        MIN(CASE WHEN e.event_type = 'payment_initiated' THEN e.occurred_at END),
        MIN(CASE WHEN e.event_type = 'payment_processed' THEN e.occurred_at END),
        MIN(CASE WHEN e.event_type = 'payment_failed'    THEN e.occurred_at END),
        MIN(CASE WHEN e.event_type = 'settled'           THEN e.occurred_at END),
        MAX(CASE WHEN e.event_type = 'settled'           THEN e.occurred_at END),
        COUNT(CASE WHEN e.event_type = 'settled'         THEN 1 END),
        COUNT(*),
        MIN(e.occurred_at),
        MAX(e.occurred_at)
    FROM payment_events e
    {where}
    GROUP BY e.transaction_id
    ON DUPLICATE KEY UPDATE
        initiated_at        = VALUES(initiated_at),
        processed_at        = VALUES(processed_at),
        failed_at           = VALUES(failed_at),
        first_settled_at    = VALUES(first_settled_at),
        last_settled_at     = VALUES(last_settled_at),
        settled_event_count = VALUES(settled_event_count),
        event_count         = VALUES(event_count),
        first_event_at      = VALUES(first_event_at),
        last_event_at       = VALUES(last_event_at)
"""

# Scoped to the transactions a batch touched — the hot path.
_RECOMPUTE = text(
    _RECOMPUTE_TEMPLATE.format(where="WHERE e.transaction_id IN :transaction_ids")
).bindparams(bindparam("transaction_ids", expanding=True))

# The same statement with no WHERE: rebuilds every transaction from the whole log.
# Sharing one template is the point — a rebuild that drifted from the incremental
# recompute would produce a *different* projection, which would make the two
# disagree about what the log means and quietly defeat the whole design.
_RECOMPUTE_ALL = text(_RECOMPUTE_TEMPLATE.format(where=""))


def ingest_events(conn: Connection, events: Sequence[EventIn]) -> IngestResponse:
    """Ingest a batch of events idempotently.

    Everything here runs inside the caller's DB transaction (see app/db.py), so
    the event insert and the recompute commit together or not at all.

    Returns a per-event outcome. A duplicate is a SUCCESS, not an error: the
    caller's intent — "this event should be on record" — holds either way. See
    README § Idempotency.
    """
    if not events:
        return IngestResponse(received=0, ingested=0, duplicates=0, results=[])

    # mode="json" renders UUID/datetime/Decimal as strings. Decimal-as-string is
    # what keeps money exact: it goes straight into DECIMAL(14,2) without ever
    # being materialised as a float.
    rows = [e.model_dump(mode="json") for e in events]

    # -- label lookup (cosmetic only; see _EXISTING_EVENT_IDS) ------------------
    event_ids = list({r["event_id"] for r in rows})
    already: set[str] = {
        str(r.event_id)
        for r in conn.execute(_EXISTING_EVENT_IDS, {"event_ids": event_ids}).fetchall()
    }

    # -- 1. merchants (FK parents) ---------------------------------------------
    merchants = {(r["merchant_id"], r["merchant_name"]) for r in rows}
    conn.execute(
        _UPSERT_MERCHANT,
        [{"merchant_id": m, "merchant_name": n} for m, n in merchants],
    )

    # -- 2. events (the idempotency boundary) ----------------------------------
    conn.execute(
        _INSERT_EVENTS,
        [
            {
                "event_id": r["event_id"],
                "transaction_id": r["transaction_id"],
                "merchant_id": r["merchant_id"],
                "event_type": r["event_type"],
                "amount": r["amount"],
                # Wire format says `timestamp`; the DB distinguishes occurred_at
                # (sender's clock) from received_at (ours). Rename at the boundary.
                "occurred_at": r["timestamp"],
                "currency": r["currency"],
                "payload": _json(r),
            }
            for r in rows
        ],
    )

    # -- 3. recompute every touched transaction from the (now correct) log ------
    txn_ids = list({r["transaction_id"] for r in rows})
    conn.execute(_RECOMPUTE, {"transaction_ids": txn_ids})

    # -- response --------------------------------------------------------------
    # Labelled per submitted position, not per distinct event_id: if a batch
    # contains the same event twice, the first reads "ingested" and the second
    # "duplicate". That is the honest description, and it keeps
    # received == len(results) so the counts always reconcile.
    results: list[EventResult] = []
    seen: set[str] = set()
    for e in events:
        eid = str(e.event_id)
        is_new = eid not in already and eid not in seen
        seen.add(eid)
        results.append(
            EventResult(
                event_id=e.event_id,
                transaction_id=e.transaction_id,
                status=IngestOutcome.INGESTED if is_new else IngestOutcome.DUPLICATE,
            )
        )

    ingested = sum(r.status == IngestOutcome.INGESTED for r in results)
    return IngestResponse(
        received=len(results),
        ingested=ingested,
        duplicates=len(results) - ingested,
        results=results,
    )


def rebuild_projection(conn: Connection) -> int:
    """Rebuild the ENTIRE transactions projection from the event log.

    Exists because the projection is derived, and a derived thing should be
    provably re-derivable rather than merely claimed to be. This is the same
    recompute used on every ingest, minus the WHERE — proof that `transactions`
    holds no information the log doesn't.

    Returns the number of transactions rebuilt.
    """
    conn.execute(text("DELETE FROM transactions"))
    conn.execute(_RECOMPUTE_ALL)
    return conn.execute(text("SELECT COUNT(*) FROM transactions")).scalar_one()


def _json(record: dict) -> str:
    return json.dumps(record, separators=(",", ":"))
