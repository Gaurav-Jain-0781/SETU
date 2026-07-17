# Data model

Three tables. **Full DDL with rationale inline: [`sql/schema.sql`](../sql/schema.sql).**

```
merchants        5 rows. The name has one home.
payment_events   append-only. THE TRUTH. Never updated, never deleted.
transactions     derived projection. Rebuildable from the log.
```

## `merchants`

Events carry `merchant_name` on every message — 10,165 events, 5 merchants. Normalising
means a rename is one `UPDATE` rather than thousands, with no window where the database
disagrees with itself.

## `payment_events` — the truth

| column | notes |
|---|---|
| `event_id` | **PRIMARY KEY** — this single line is the idempotency mechanism |
| `event_type` | `CHECK` constrained to the four known types |
| `amount` | `DECIMAL(14,2)` — **never** `FLOAT` |
| `occurred_at` | when it happened, per the sender's clock |
| `received_at` | when *we* durably accepted it |
| `payload` | raw JSON as received, for forensics and replay |

**`occurred_at` vs `received_at`.** We're a receiver; events cross networks from systems
with their own clocks. If a partner replays six months of history today, `occurred_at`
says January and `received_at` says July. Reconciliation needs the first; debugging "why
was this late?" needs the second. Collapsing them loses information you can't recover.

**`DECIMAL(14,2)`, never `FLOAT`.** `0.1 + 0.2 == 0.30000000000000004` in binary floating
point, and errors compound across thousands of rows. A service whose entire job is
checking whether numbers match cannot use a type that invents fractions of a paisa.

**`DATETIME(6)` storing UTC.** MySQL's `TIMESTAMP` is timezone-aware but only spans
1970–2038 — a [Y2038](https://en.wikipedia.org/wiki/Year_2038_problem) cliff in a
payments system is a bad trade. `DATETIME(6)` has no timezone awareness, so we store UTC
by convention and convert at the edges. A real tradeoff: a DB-enforced guarantee swapped
for an application-maintained discipline.

## `transactions` — the projection

**Stores facts, not a status.**

| column | meaning |
|---|---|
| `initiated_at` | earliest `payment_initiated` — `NULL` if never |
| `processed_at` | earliest `payment_processed` — `NULL` if never |
| `failed_at` | earliest `payment_failed` — `NULL` if never |
| `first_settled_at` / `last_settled_at` | settlement window |
| `settled_event_count` | how many **distinct** settlement events |

`NULL` is load-bearing: `failed_at IS NULL` is the positive fact *"we have never seen
this payment fail"*, and every discrepancy rule is built on it.

### Status is a generated column

```sql
payment_status VARCHAR(16) GENERATED ALWAYS AS (
    CASE WHEN failed_at    IS NOT NULL THEN 'failed'
         WHEN processed_at IS NOT NULL THEN 'processed'
         ELSE 'pending' END
) STORED
```

You can't insert into it — MySQL computes it and keeps it current.

**Why not compute it in Python?** Because this makes drift *impossible*. If application
code owned the status, every path touching `failed_at` would have to remember to
recompute it — a bugfix, a backfill, someone running SQL against prod. Miss one and you
get a row reading `status='processed'` with `failed_at` set: a row that lies about
itself. In a reconciliation system that's the worst possible bug, because "tell me the
truth about my money" *is* the product.

Not "we're careful" — structurally cannot.

## Two status axes, not one

The most important schema decision.

The obvious design is one column: `status ENUM('initiated','processed','failed','settled')`.
**It makes the assignment's core requirement unrepresentable.** "Settlement recorded for
a failed payment" means a transaction is *both* failed *and* settled. With one column,
`settled` overwrites `failed` and the evidence is destroyed — you cannot query for what
you just erased. Those 95 transactions would be indistinguishable from healthy ones.

So they're independent, and discrepancies are the **illegal cells of a matrix**:

| | `unsettled` | `settled` | `settled` ×N |
|---|---|---|---|
| **`pending`** | ⚠️ 190 stuck | ⚠️ settled-unprocessed | ⚠️ |
| **`processed`** | ⚠️ 380 past SLA | ✅ 2,470 healthy | ⚠️ 95 double-settled |
| **`failed`** | ✅ 570 clean fail | ⚠️ 95 settled-a-failure | ⚠️ |

A third `status` column exists purely as a convenience filter for
`GET /transactions?status=`. The two axes above are what reconciliation reasons over.

## The sample data

Used unmodified: 10,355 events, 5 merchants, 3,800 transactions, 2026-01-08 → 2026-04-08.

After deduplication every transaction falls into exactly six shapes:

| post-dedup sequence | txns | meaning |
|---|---|---|
| initiated → processed → settled | 2,470 | healthy |
| initiated → failed | 570 | clean failure |
| initiated → processed | 380 | **processed, never settled** |
| initiated | 190 | **stuck** |
| initiated → processed → settled → settled | 95 | **double settlement** |
| initiated → failed → settled | 95 | **settled a failed payment** |

Verified: no transaction ever changes merchant or amount across its events, and no
`settled` is ever timestamped before its `payment_processed`. The data is internally
consistent — the discrepancies are semantic, not corrupt.
