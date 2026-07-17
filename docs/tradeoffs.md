# Tradeoffs and assumptions

## Assumptions

1. **`event_id` is globally unique and stable across retries.** The entire idempotency
   guarantee rests on this. If a sender minted a fresh `event_id` per retry, no receiver
   could distinguish a retry from a real second event.
2. **All events for one transaction agree on `merchant_id`, `amount` and `currency`.**
   Verified across all 10,355 sample events. First observation wins; these are treated as
   invariants of the transaction, not merge targets.
3. **Naive timestamps are UTC.** The sample data is all timezone-aware; a naive value is
   assumed UTC rather than rejected, since rejecting mid-integration is a worse failure
   mode than a documented assumption.
4. **`payment_failed` is terminal and beats `payment_processed`** if both were ever seen.
   Conservative: don't claim the money is good. No such row exists in the sample data.
5. **The date dimension is the UTC date of `payment_initiated`** — the transaction's
   business date.
6. **A single SLA governs both time-relative rules.** In reality, processing and
   settlement would have different SLAs; both sample cohorts are far past 24h either way.
7. **No authentication.** Out of scope for the assignment. In production this endpoint
   needs at minimum a shared secret or HMAC signature verification — an unauthenticated
   ingestion endpoint lets anyone forge settlements.

## Tradeoffs

| Choice | Cost | Why anyway |
|---|---|---|
| **MySQL over PostgreSQL** | No `RETURNING`, no partial indexes, no `FILTER`, no tz-aware timestamp | Familiarity, and a stack I can defend end to end. The `RETURNING` constraint forced a *simpler, better* design — see below. |
| **Offset pagination** | `OFFSET 100000` walks and discards 100k rows | Ops wants "page 4 of 76, 3,800 results" — keyset gives neither random access nor totals. At 3,800 rows offset is free. Past ~100k this should change. |
| **`CHAR(36)` UUIDs** | 20 bytes/row more than `BINARY(16)`, slower to index | Readable by hand, no `UUID_TO_BIN`/`BIN_TO_UUID` noise on every query. A reconciliation tool is one people read. Flips at tens of millions of rows. |
| **`DATETIME(6)` storing UTC** | No DB-enforced timezone guarantee | `TIMESTAMP` only spans 1970–2038. A Y2038 cliff in a payments system is a bad trade. The cost is real: a guarantee swapped for an app-maintained discipline. |
| **No Alembic** | No migration history | One greenfield schema has nothing to migrate. `sql/schema.sql` is idempotent DDL applied on boot, so a fresh container self-provisions. A schema with real history would need it. |
| **`auto_migrate` on boot** | DDL runs on every start | Idempotent (`CREATE TABLE IF NOT EXISTS`), and it means no manual migrate step between "deploy" and "works" — the difference between a reviewer seeing the service and seeing a 500. |

### What MySQL cost, specifically

- **No `RETURNING`** — drove the recompute design. This turned out *better* (simpler,
  self-evidently correct), so the constraint helped. See [Idempotency](idempotency.md).
- **No partial indexes.** Postgres can index *only the broken rows*
  (`WHERE processed_at IS NOT NULL AND first_settled_at IS NULL` — ~380 of 3,800),
  making discrepancy queries near-instant off a tiny index. MySQL has no equivalent.
  Irrelevant at 3,800 rows; at millions I'd need a materialised discrepancy table
  refreshed on a schedule — which reintroduces exactly the staleness we avoided.
- **No `FILTER`** — conditional aggregates use `COUNT(CASE WHEN … THEN 1 END)`. Same
  result, noisier.
- **TiDB silently ignores `CHECK`** unless enabled. See [Deployment](deployment.md).

## Indexes: measured, and two were removed

Indexes aren't free — each is extra work on the hot ingestion path, and this service
writes far more often than it reads. Every index was benchmarked before being kept.

| index | measured at 500k rows | kept |
|---|---|---|
| `(merchant_id, initiated_at DESC)` | **16×** faster at 2,000 merchants (0.75ms vs 11.9ms) | ✅ |
| `(initiated_at DESC)` | the planner's default choice for almost every list query | ✅ |
| `(status, initiated_at DESC)` | **slower** than the plan already chosen (0.072ms vs 0.048ms) — only ~4 distinct statuses, so the filter is cheap and `LIMIT` stops the scan early | ❌ removed |
| `(merchant_id, status, initiated_at DESC)` | 1.7× on an already-sub-1ms query | ❌ removed |

The merchant composite looks useless against the sample's **5 merchants** — a 1-of-5
filter isn't selective, so the planner correctly ignores it. It exists for production
merchant cardinality, where the logic inverts. That's why it was benchmarked at 2,000
merchants rather than 5.

**Shipping two indexes I can justify beats shipping four I can't.**

## Not done, deliberately

- **Amount-mismatch detection.** The classic real-world reconciliation discrepancy —
  settled amount ≠ processed amount (partial settlement, fees deducted). The sample data
  has none (verified), so it would be untested code against imaginary data. Would be a
  sixth rule.
- **Authentication.** See Assumptions.
- **Rate limiting / backpressure.** A real ingestion endpoint needs both.
- **CI.** No GitHub Actions workflow; tests run locally.

## With more time

1. **Amount-mismatch rule**, with generated test data.
2. **A `POST /admin/rebuild-projection` endpoint.** The projection is rebuildable by
   design and `rebuild_projection()` exists with a test — but the capability should be
   exposed and exercised, not just claimed.
3. **Materialised summary rollups.** `GROUP BY` over the full table is fine at 3,800 rows
   and won't be at 50M. Daily per-merchant rollups, incrementally maintained.
4. **Structured logging + request IDs.** Currently plain log lines.
5. **CI** running the suite against real MySQL on every push.
