-- =============================================================================
-- Setu reconciliation service — MySQL 8 schema
--
-- Rationale lives in README.md; the short version:
--
--   payment_events   append-only source of truth. Never updated, never deleted.
--   transactions     projection DERIVED from payment_events. Can be dropped and
--                    rebuilt from the log at any time (see the recompute in
--                    app/ingest.py, minus its WHERE clause).
--
-- The projection stores MILESTONE FACTS (timestamps + counts), never a status.
-- Status is a GENERATED column computed by MySQL from those facts, so it cannot
-- drift from the evidence it summarises.
--
-- Every statement is idempotent (CREATE ... IF NOT EXISTS) so this file can be
-- applied on every boot.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- merchants
--
-- Events carry merchant_name on every message: 10,165 events, 5 merchants.
-- Normalising means the name has one home — a rename is one UPDATE rather than
-- thousands, with no window where the database disagrees with itself.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id   VARCHAR(64)  NOT NULL,
    merchant_name VARCHAR(255) NOT NULL,
    first_seen_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at    DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                               ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (merchant_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- -----------------------------------------------------------------------------
-- payment_events — the truth
--
-- PRIMARY KEY (event_id) IS the idempotency mechanism. The database itself
-- refuses a second row for the same event_id, so correctness does not depend on
-- application code remembering to check — and, critically, there is no window
-- between "check" and "write" for two concurrent replays to race through.
-- INSERT IGNORE turns that guarantee into a cheap silent no-op.
--
-- occurred_at vs received_at: occurred_at is the sending system's clock
-- (authoritative for reconciliation windows); received_at is ours (when we
-- durably accepted it). We are a receiver — events cross networks from systems
-- with their own clocks. Keeping both is what lets you debug clock skew, late
-- delivery and backfills instead of guessing.
--
-- Why DATETIME(6) and not TIMESTAMP: TIMESTAMP is timezone-aware but only spans
-- 1970-2038. Building a Y2038 cliff into a payments system is a bad trade.
-- DATETIME(6) has no timezone awareness, so we store UTC by convention and
-- convert at the edges in Python (app/schemas.py normalises every inbound
-- timestamp to UTC). This is a real tradeoff: a DB-enforced guarantee swapped
-- for an application-maintained discipline.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment_events (
    -- CHAR(36) not BINARY(16): readable in a client, no UUID_TO_BIN/BIN_TO_UUID
    -- noise on every query. Costs ~20 bytes/row. A reconciliation tool is one
    -- people read by hand; at tens of millions of rows this would flip.
    event_id       CHAR(36)      NOT NULL,
    transaction_id CHAR(36)      NOT NULL,
    merchant_id    VARCHAR(64)   NOT NULL,
    event_type     VARCHAR(32)   NOT NULL,
    -- DECIMAL, never FLOAT/DOUBLE. Binary floating point cannot represent 15248.29
    -- exactly and the error compounds across a SUM of thousands of rows. A service
    -- whose entire job is checking whether numbers match cannot use a type that
    -- invents fractions of a paisa. 14 digits covers ~1e12.
    amount         DECIMAL(14,2) NOT NULL,
    currency       CHAR(3)       NOT NULL,
    occurred_at    DATETIME(6)   NOT NULL,
    received_at    DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    -- Raw payload kept verbatim for forensics and replay. The typed columns above
    -- cover today's contract; this keeps the log a faithful record of what we were
    -- actually sent rather than what we understood at the time.
    payload        JSON          NOT NULL,

    PRIMARY KEY (event_id),

    CONSTRAINT fk_events_merchant
        FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id),

    -- CHECK is enforced from MySQL 8.0.16 (older versions parsed and silently
    -- ignored it). Defence in depth only — Pydantic rejects unknown event types
    -- before they ever reach the database. NOTE: TiDB may not enforce CHECK
    -- depending on version/settings, which is exactly why this is the second line
    -- of defence and not the only one.
    CONSTRAINT chk_event_type CHECK (event_type IN
        ('payment_initiated', 'payment_processed', 'payment_failed', 'settled')),
    CONSTRAINT chk_event_amount CHECK (amount >= 0),

    -- Serves two things at once:
    --  1. the recompute in ingest.py — "give me this transaction's events"
    --  2. GET /transactions/{id} event history, already in time order
    -- Because the index is stored sorted by (transaction_id, occurred_at), the
    -- ORDER BY is satisfied by index order and never needs a sort.
    KEY idx_events_txn_time (transaction_id, occurred_at),

    -- "Recent events for a merchant" operational lookups; also keeps the FK cheap.
    KEY idx_events_merchant_time (merchant_id, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- -----------------------------------------------------------------------------
-- transactions — the projection
--
-- One row per transaction, rebuilt by the recompute in app/ingest.py after every
-- ingest. NOT maintained incrementally: we never add to these columns, we
-- overwrite them with a fresh answer derived from the event log. That is what
-- makes ingestion idempotent and order-independent without needing to know which
-- events were new (MySQL has no RETURNING to tell us).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id      CHAR(36)      NOT NULL,
    merchant_id         VARCHAR(64)   NOT NULL,
    amount              DECIMAL(14,2) NOT NULL,
    currency            CHAR(3)       NOT NULL,

    -- Milestone facts. NULL is load-bearing: it is the positive assertion
    -- "we have never seen this happen", and every discrepancy rule is built on it.
    -- These fall out of the recompute for free — MIN(CASE WHEN ...) over zero
    -- matching rows is NULL.
    initiated_at        DATETIME(6)   NULL,
    processed_at        DATETIME(6)   NULL,
    failed_at           DATETIME(6)   NULL,
    first_settled_at    DATETIME(6)   NULL,
    last_settled_at     DATETIME(6)   NULL,

    -- Distinct settlement EVENTS. >1 means genuinely different events settled this
    -- transaction twice — not the same event replayed, which INSERT IGNORE absorbs
    -- before the recompute ever counts it. In the sample data that distinction is
    -- worth 67 false positives (162 apparent vs 95 real).
    settled_event_count INT           NOT NULL DEFAULT 0,
    event_count         INT           NOT NULL DEFAULT 0,

    first_event_at      DATETIME(6)   NOT NULL,
    last_event_at       DATETIME(6)   NOT NULL,
    updated_at          DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                      ON UPDATE CURRENT_TIMESTAMP(6),

    -- ---- Derived state ------------------------------------------------------
    -- GENERATED ALWAYS ... STORED: computed by MySQL, indexable like any column,
    -- and structurally incapable of contradicting the timestamps above. If Python
    -- owned these, every path touching failed_at would have to remember to
    -- recompute them — a bugfix, a backfill, someone running SQL against prod.
    -- Miss one and you get a row saying 'processed' with failed_at set: a row that
    -- lies about itself. In a reconciliation system that is the worst possible bug.
    --
    -- Payment and settlement are INDEPENDENT axes. Collapsing them into one enum
    -- would make "failed AND settled" unrepresentable — settled would overwrite
    -- failed and destroy the very evidence /reconciliation/discrepancies exists to
    -- surface (95 rows in the sample data).
    --
    -- failed_at beats processed_at if both were somehow seen: the conservative
    -- reading is that the money is not good. No such row exists in the sample.
    payment_status VARCHAR(16) GENERATED ALWAYS AS (
        CASE
            WHEN failed_at    IS NOT NULL THEN 'failed'
            WHEN processed_at IS NOT NULL THEN 'processed'
            ELSE 'pending'
        END
    ) STORED,

    settlement_status VARCHAR(16) GENERATED ALWAYS AS (
        CASE
            WHEN first_settled_at IS NOT NULL THEN 'settled'
            ELSE 'unsettled'
        END
    ) STORED,

    -- Flattened convenience axis for GET /transactions?status=. The two axes above
    -- are what reconciliation actually reasons over.
    status VARCHAR(16) GENERATED ALWAYS AS (
        CASE
            WHEN first_settled_at IS NOT NULL THEN 'settled'
            WHEN failed_at        IS NOT NULL THEN 'failed'
            WHEN processed_at     IS NOT NULL THEN 'processed'
            ELSE 'pending'
        END
    ) STORED,

    PRIMARY KEY (transaction_id),

    CONSTRAINT fk_txn_merchant
        FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id),

    CONSTRAINT chk_txn_amount CHECK (amount >= 0),
    CONSTRAINT chk_txn_counts CHECK (settled_event_count >= 0 AND event_count >= 0),

    -- A transaction must have been observed somehow. Guards against a row
    -- materialising with no evidence behind it.
    CONSTRAINT chk_txn_has_evidence CHECK (
        initiated_at IS NOT NULL OR processed_at IS NOT NULL
        OR failed_at IS NOT NULL OR first_settled_at IS NOT NULL
    ),

    -- ---- Indexes -------------------------------------------------------------
    -- Every index here was benchmarked before being kept; two candidates were
    -- measured and REMOVED (see README § Index tuning). Indexes are not free —
    -- each is extra work on the hot ingestion path, and this service writes far
    -- more often than it reads.
    --
    -- Removed after measurement at 500k rows:
    --   (status, initiated_at DESC)              measurably SLOWER than the plan
    --                                            already chosen. Only ~4 distinct
    --                                            statuses, so the filter is cheap
    --                                            and LIMIT stops the scan early.
    --   (merchant_id, status, initiated_at DESC) 1.7x on a sub-1ms query. Not
    --                                            worth the write cost.
    --
    -- NOTE: MySQL has no partial indexes. Postgres can index ONLY the broken rows
    -- (~380 of 3,800), making discrepancy queries near-instant off a tiny index.
    -- No equivalent exists here — irrelevant at this size, but at millions of rows
    -- this is where MySQL costs us. See README § Tradeoffs.
    --
    -- DESC in an index is real from MySQL 8.0 (before that it was parsed and
    -- ignored).

    -- The workhorse: date-range and recency scans. With the sample's low merchant
    -- cardinality the planner picks this for almost every list query.
    KEY idx_txn_initiated (initiated_at DESC),

    -- Merchant filter + recency sort, served from the index with no sort node.
    -- Looks redundant at the sample's 5 merchants — and it is, there: a 1-of-5
    -- filter is not selective, so the planner correctly ignores it. It is sized
    -- for production merchant cardinality, where the logic inverts. Measured at
    -- 500k rows / 2,000 merchants: chosen automatically, 0.75ms vs 11.9ms — 16x.
    KEY idx_txn_merchant_initiated (merchant_id, initiated_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
