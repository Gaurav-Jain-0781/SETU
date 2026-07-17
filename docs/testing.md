# Testing

**46 tests, passing on both MySQL 8.4 and TiDB.**

```bash
docker compose up -d db     # tests need a real MySQL
pytest -q
```

Against TiDB:

```bash
TEST_ADMIN_URL="mysql+pymysql://<user>:<pass>@gateway01.<region>.prod.aws.tidbcloud.com:4000" \
DB_SSL=true pytest -q
```

## Real MySQL, never SQLite

Tests run against a real engine in a separate `setu_test` database, created and dropped
by the suite so dev data is never touched.

Half of what this service relies on is engine-specific: generated columns, `INSERT
IGNORE` semantics, `CHECK` enforcement, window functions, `DECIMAL` behaviour. **A suite
passing on SQLite would be testing a different program than the one we ship.**

The suite applies `sql/schema.sql` — the same DDL production runs. Tests against a
hand-maintained test schema silently stop testing the real one the first time they drift.

## What's covered

| file | covers |
|---|---|
| `test_idempotency.py` | replay, batch-internal duplicates, byte-identical state after full replay, projection rebuild |
| `test_reconciliation.py` | all 5 rules, SLA boundary, out-of-order arrival, value reconciliation |
| `test_api.py` | validation, error envelope, pagination, filtering, schema-level guarantees |

## The tests that matter most

**A matched pair.** Either alone is misleading:

- **`test_replayed_settlement_does_not_inflate_count`** — replays a `settled` ten times
  and asserts the count stays 1. This is the bug that turns 95 real double-settlements
  into a phantom 162.
- **`test_distinct_settlement_events_are_counted`** — two settlements with *different*
  `event_id`s must still be caught. An implementation that deduplicated on
  `(transaction_id, event_type)` would pass the first test and fail this one, silently
  hiding real financial errors.

**`test_events_converge_regardless_of_arrival_order`** — the same three events in four
different orders, all producing an identical row. Parametrised, because "we handle
out-of-order events" is worth proving rather than claiming.

**`test_sla_boundary`** — asserts a transaction is clean at 23h and a discrepancy at 25h.
Deterministic because of `?as_of=`; otherwise it would need to sleep for a day.

## Schema-level tests bypass the app

These assert against the **database**, not the API — proving the guarantees hold against
someone running raw SQL, which is the whole point of enforcing them in the schema rather
than in Python.

- `test_check_constraint_rejects_unknown_event_type` — ✅ on MySQL 8.4. **This is the
  canary that caught TiDB**: it failed there, revealing that TiDB ignores `CHECK` unless
  `tidb_enable_check_constraint = ON`. Found before deploying, not from a corrupt row in
  production. See [Deployment](deployment.md).
- `test_generated_status_cannot_be_written` — `UPDATE transactions SET status=...` is
  refused. Nobody can put a lie in that column.
- `test_duplicate_event_id_is_refused_by_the_primary_key` — idempotency enforced by the
  database, not by code that could race.

## Postman

[`postman_collection.json`](../postman_collection.json) — **26 requests, 55 assertions**,
verified against the live deployment with newman:

```bash
npx newman run postman_collection.json
```

Ordered as a story, not an index: folder 2 proves idempotency by replaying an event and
showing the count move by zero; folder 4 walks the discrepancy classes.

Running it (rather than assuming it worked) found three real problems: `+00:00` in a
query string parses as a space and 422s the date filters; assertions on exact row counts
break once the collection's own demo transaction lands; and the SQL-injection probe
returns 403 from Render's edge WAF rather than our 422.

⚠️ **Point it at localhost, not production**, if you're about to demo — folder 3 ingests
a transaction, so the live counts drift by one per run.

## No CI

There is deliberately no GitHub Actions workflow — run `pytest` locally against a live
MySQL. Wiring CI is the obvious next step; see [Tradeoffs](tradeoffs.md).
