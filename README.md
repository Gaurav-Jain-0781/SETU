# Setu Reconciliation Service

A backend service that ingests payment lifecycle events, maintains transaction
state, and reports reconciliation discrepancies.

**Live demo:** _TBD — deployment pending_
**Interactive API docs:** `/docs` on the deployed URL (auto-generated, click "Try it out")

---

## Table of contents

- [What this service is](#what-this-service-is)
- [Architecture](#architecture)
- [Data model](#data-model)
- [Idempotency — the core of the design](#idempotency--the-core-of-the-design)
- [Discrepancy rules](#discrepancy-rules)
- [Local setup](#local-setup)
- [API documentation](#api-documentation)
- [Deployment](#deployment)
- [Testing](#testing)
- [Assumptions](#assumptions)
- [Tradeoffs and what I'd do differently](#tradeoffs-and-what-id-do-differently)
- [AI tool disclosure](#ai-tool-disclosure)

---

## What this service is

Setu provides payment infrastructure. A *partner* builds on top of it and receives
payment lifecycle events from several systems — Setu's own rails, banks, their own
gateway. This service is the partner's: it receives those events, works out where
each payment stands, and tells the ops team which ones look wrong.

**The single most important framing: we are a receiver, not a sender.**

```
   Setu's payment system  ──┐
   Bank / NPCI            ──┼──►  POST /events  ──►  THIS SERVICE  ──►  MySQL
   Partner's gateway      ──┘                             │
                                                          ▼
                                      GET /transactions, /reconciliation/*
                                                          │
                                                          ▼
                                                Partner's ops team
```

Two consequences follow from that, and they drive every decision below:

**1. Duplicate events are structural, not a bug.** A sender delivers an event, our
`200 OK` is lost to a network blip, and the sender — behaving *correctly* — retries.
It genuinely cannot know whether we received it. Every serious webhook sender does
this. Duplicates cannot be engineered away upstream, so they must be handled here.
The bundled sample data contains **190** of them.

**2. Event order is not guaranteed.** Those systems don't coordinate with each
other. Different networks, different retry timers, different clocks. A `settled`
can arrive before its `payment_processed`. Any design that assumes ordering will
corrupt itself in production.

---

## Architecture

```
                    ┌──────────────────────────────────────────┐
   POST /events ───►│  FastAPI + Pydantic (validation)         │
                    └───────────────────┬──────────────────────┘
                                        │  one DB transaction
                    ┌───────────────────▼──────────────────────┐
                    │  1. INSERT IGNORE  →  payment_events     │
                    │     (append-only. THE TRUTH.)            │
                    │  2. recompute      →  transactions       │
                    │     (derived. A CACHE OF CONCLUSIONS.)   │
                    └───────────────────┬──────────────────────┘
                                        │
                    ┌───────────────────▼──────────────────────┐
   GET  /... ──────►│  Read-time SQL: filter, aggregate, rules │
                    └──────────────────────────────────────────┘
```

### Why two tables

| Approach | Verdict |
|---|---|
| Store only current state, update in place | ✗ Destroys history. Cannot answer "was this settled twice?" — the assignment requires history preserved. |
| Store only events, compute state on every read | ✗ Correct but every list/summary query re-derives all 3,800 transactions from 10,165 events. Doesn't scale. |
| **Store both: events as truth, transactions as a derived projection** | ✓ Pay the cost once per event (~10k times) instead of once per question (constantly). |

`payment_events` is append-only and never modified. `transactions` is **derived** —
which means it can be thrown away and rebuilt from the event log at any time. The
truth is never the thing that can break.

### Layout

```
app/
  main.py          FastAPI app, error envelope
  config.py        env-driven settings
  db.py            engine, pool, connection dependency
  schemas.py       Pydantic request/response contracts
  ingest.py        idempotent ingestion  ← the heart
  queries.py       every read query, in one auditable file
  routers/         one module per endpoint group
sql/schema.sql     hand-written DDL + indexes, commented
scripts/           sample data loader
tests/             idempotency, discrepancy rules, API contract
```

Two deliberate choices:

**`sql/schema.sql` is hand-written DDL, not ORM-generated.** The schema should be
readable *as SQL*, with its indexes and their justifications visible in one file.

**All SQL lives in `queries.py`, not scattered across routers.** A reviewer can
audit every query the service runs by opening one file.

**No ORM.** An ORM would hide the schema, indexes and queries — the things most
worth reviewing here. Aggregation, filtering, sorting and pagination all happen in
SQL; Python never receives a row it intends to discard and never sums a column.

---

## Data model

Three tables. Full DDL with inline rationale: [`sql/schema.sql`](sql/schema.sql).

### `merchants`

One row per merchant. Events carry `merchant_name` denormalised on every message —
10,165 events, 5 merchants. Storing the name once means a rename is one `UPDATE`
rather than thousands, with no window where the database disagrees with itself.

### `payment_events` — the truth

| column | notes |
|---|---|
| `event_id` | **PRIMARY KEY** — this single line is the idempotency mechanism |
| `transaction_id`, `merchant_id` | FK to `merchants` |
| `event_type` | `CHECK` constrained to the four known types |
| `amount` | `DECIMAL(14,2)` — **never** `FLOAT` |
| `occurred_at` | when it happened, per the sender's clock |
| `received_at` | when *we* durably accepted it |
| `payload` | raw JSON as received, for forensics and replay |

**`occurred_at` vs `received_at`.** We're a receiver; events cross networks from
systems with their own clocks. If a partner replays six months of history today,
`occurred_at` says January and `received_at` says July. Reconciliation logic needs
the first; debugging "why was this late?" needs the second. Collapsing them loses
information that cannot be recovered.

**Money is `DECIMAL(14,2)`, never `FLOAT`.** `0.1 + 0.2 == 0.30000000000000004` in
binary floating point. Errors compound across thousands of rows. A reconciliation
service whose job is checking whether numbers match cannot use a type that invents
fractions of a paisa.

**Time is `DATETIME(6)` storing UTC.** MySQL's `TIMESTAMP` is timezone-aware but
only spans 1970–2038 — putting a [Y2038 cliff](https://en.wikipedia.org/wiki/Year_2038_problem)
into a payments system is a bad trade. `DATETIME(6)` has no timezone awareness, so
we store UTC by convention and convert at the edges in Python. This is a real
tradeoff: a DB-enforced guarantee swapped for an application-maintained discipline.

### `transactions` — the projection

**This table stores facts, not a status.**

| column | meaning |
|---|---|
| `initiated_at` | earliest `payment_initiated` seen — `NULL` if never |
| `processed_at` | earliest `payment_processed` — `NULL` if never |
| `failed_at` | earliest `payment_failed` — `NULL` if never |
| `first_settled_at` / `last_settled_at` | settlement window |
| `settled_event_count` | how many **distinct** settlement events |

`NULL` is load-bearing here: `failed_at IS NULL` is the positive fact *"we have
never seen this payment fail"*, and every discrepancy rule is built from it.

Status is then a **generated column** — computed by MySQL from those facts:

```sql
payment_status VARCHAR(16) AS (
    CASE WHEN failed_at    IS NOT NULL THEN 'failed'
         WHEN processed_at IS NOT NULL THEN 'processed'
         ELSE 'pending' END
) STORED
```

**Why generated rather than computed in Python?** Because it makes drift
impossible. If application code owned the status, every path that touches
`failed_at` would have to remember to recompute it — a bugfix, a backfill, someone
running SQL against prod. Miss one and you get a row reading `status='processed'`
with `failed_at` set: a row that lies about itself. In a reconciliation system, a
row that lies is the worst possible bug, because "tell me the truth about my money"
*is* the product. A generated column cannot desynchronise — not "we're careful",
structurally cannot.

### Payment and settlement are independent axes

The single most important schema decision.

The obvious design is one column: `status ENUM('initiated','processed','failed','settled')`.
**It makes the assignment's core requirement unrepresentable.** "Settlement recorded
for a failed payment" means a transaction is *both* failed *and* settled. With one
column, `settled` overwrites `failed` and the evidence is destroyed — you cannot
query for what you just erased. Those 95 transactions would be indistinguishable
from healthy ones.

So they are separate, and discrepancies are the **illegal cells of a matrix**:

| | `unsettled` | `settled` | `settled` ×N |
|---|---|---|---|
| **`pending`** | ⚠️ 190 stuck | ⚠️ settled-unprocessed | ⚠️ |
| **`processed`** | ⚠️ 380 past SLA | ✅ 2,470 healthy | ⚠️ 95 double-settled |
| **`failed`** | ✅ 570 clean fail | ⚠️ 95 settled-a-failure | ⚠️ |

A third `status` column exists purely as a convenience filter for
`GET /transactions?status=`. The two axes above are what reconciliation reasons over.

---

## Idempotency — the core of the design

> *"Submitting the same event twice should not create duplicate records or corrupt
> transaction state."*

### The mechanism: a primary key, not application code

```sql
PRIMARY KEY (event_id)
```

The obvious approach is broken:

```python
if not event_exists(event_id):   # ← two concurrent replays both pass this
    insert(event)                # ← ...and both land here
```

The check and the write are separate steps, and the world can change between them.
Two copies arriving simultaneously both see "doesn't exist" and both insert. No
amount of application-level care closes that window.

A primary key makes checking and writing **the same operation**. `INSERT IGNORE`
means the database decides, atomically. There is no in-between to race through.

### The harder half: not double-counting

Storing the event once is easy. The trap is step two — updating `transactions`
*only if* the event was genuinely new. A replayed `settled` that bumps
`settled_event_count` invents a duplicate settlement that never happened.

PostgreSQL solves this with `RETURNING` (insert, skip duplicates, and hand back
which ones were actually new). **MySQL has no `RETURNING`.**

### The solution: recompute, don't increment

We never ask "was this event new?" — because we never add anything.

```
1. INSERT IGNORE the events.  Duplicates are silently dropped.
   The log is now correct — the primary key guarantees it.

2. For each touched transaction, RECOMPUTE its row by reading
   ALL of that transaction's events back out of the log.
```

Step 2 doesn't care whether the events were new. It reads the deduplicated log and
derives the answer. Fresh or replayed, the computed result is identical.

```sql
settled_event_count = COUNT(settled rows in the log)   -- recount. Cannot be wrong.
-- versus
settled_event_count = settled_event_count + 1          -- tally. Wrong on replay.
```

A recount doesn't add to anything — it overwrites with a fresh answer off the log.
Run it once or fifty times, same result.

**Two guarantees, chained:**

```
PRIMARY KEY (event_id)  →  the log holds each event exactly once
                            ↓
recompute from the log  →  the projection is always correct
```

### What this buys for free

**Out-of-order events self-heal.** A `settled` arriving before its
`payment_processed` just sets `first_settled_at`; when `processed` arrives later,
the next recompute sees both. No state machine, no "illegal transition" to reject.

**Unknown transactions are acceptable.** A `settled` for a transaction we've never
seen creates the row rather than 404-ing. In a multi-system integration you cannot
assume the initiator's events reach you first.

**Crash safety.** The `INSERT IGNORE` and the recompute run in **one DB transaction**
(`ENGINE=InnoDB`). Either both commit or both roll back — the half-state where the
log has an event the projection doesn't know about cannot be observed.

**Recoverability.** Because `transactions` is *derived*, it can be dropped and
rebuilt from the log — same recompute SQL, minus the `WHERE`. A design that
increments counters can never do this: the increments are gone and the current
number is all you have. Once wrong, wrong forever.

### Why this beats the cleverer alternative

An incremental merge (`LEAST`/`GREATEST`/`+`) is O(1) per event and never reads
events back. It's faster in theory. But:

| | incremental merge | recompute from log |
|---|---|---|
| Cost | O(1) | reads ~2.7 rows (10,165 events ÷ 3,800 txns) |
| Needs `RETURNING` | yes | **no** |
| Correct if the reasoning is subtly wrong | no | **yes — it re-derives from truth** |
| Explainable in one sentence | not really | *"recompute from the log"* |

**I chose the simpler one deliberately.** It trades a theoretical performance win
for a design whose correctness is self-evident rather than argued. The clever
version is correct *only if* my reasoning about commutative operators holds. The
simple version is correct because it reads the truth and reports it.

### Verified, not asserted

Replaying all 10,355 events against a loaded database:

```
received:   10,355
ingested:        0
duplicates: 10,355
```

An MD5 over every column of all 3,800 transaction rows is **byte-identical**
before and after. Not "approximately unchanged" — bit-for-bit. `updated_at` doesn't
move either, because with zero new events the recompute has nothing to write.

### The 190-vs-67 trap

The sample data contains 190 exact-duplicate `event_id`s (64 `initiated`, 59
`processed`, 67 `settled`) with byte-identical payloads.

Those 67 duplicated `settled` events matter enormously. **Naive ingestion reports
162 double-settlements. Only 95 are real** — the other 67 are the same settlement
delivered twice. Idempotency isn't hygiene here; get it wrong and the reconciliation
report is inflated by 70% and an ops team chases 67 problems that don't exist.

---

## Discrepancy rules

Five classes. Each is a `WHERE` clause over a **single row** — no joins, no
aggregation, no scanning event history. The schema did the work.

| type | rule | sample count |
|---|---|---|
| `processed_never_settled` | processed, unsettled, past SLA | **380** |
| `settled_despite_failure` | `failed_at` **and** `first_settled_at` both set | **95** |
| `duplicate_settlement` | `settled_event_count > 1` | **95** |
| `stuck_pending` | initiated only, past SLA | **190** |
| `settled_without_processing` | settled with no prior processing | **0** |

Live SQL for every rule is served at **`GET /reconciliation/rules`** — the actual
predicate strings the queries are built from, not a prose paraphrase that could
drift. Reconciliation output is only trustworthy if an operator can audit what the
service means by "broken".

**`settled_despite_failure` is one line** — and it's only expressible because
payment and settlement are separate axes. That single line is the whole "settlement
recorded for a failed payment" requirement.

**`settled_without_processing` matches zero rows and is kept deliberately.** It's
the canary: settlement without processing is impossible if events are honest, so a
non-zero count means *our ingestion* is broken, not the merchant's money. It's also
exactly the corruption an order-dependent state machine would manufacture from
out-of-order events.

**`duplicate_settlement` counts distinct settlement *events*.** A settlement
replayed under the same `event_id` is absorbed by ingestion and correctly **not**
reported. That's the 162-vs-95 distinction.

### Why discrepancies are computed at read time

Two rules are **time-relative**: a transaction becomes a discrepancy purely by
getting older, with no event to trigger a write. A stored `is_broken` flag would be
wrong the instant the clock crossed the SLA, unless a cron job re-swept the table
forever. Computing on read means the answer is always correct as of the moment you
ask.

`?as_of=` and `?sla_hours=` control the window — which also makes the rules testable
without sleeping for a day.

### Where the 24h SLA came from

Measured from the sample data, not guessed:

- Every settlement lands within **6.00h** of processing (p50 2.93h, p99 5.95h — a hard ceiling)
- Every never-settled transaction is **≥17.3h** old

There is a clean, wide gap between "normal" and "broken". 24h sits inside it with
4× margin over observed p99, and matches the **T+1** settlement convention in Indian
payments. It is a query parameter, so ops can tighten it without a deploy.

---

## Local setup

**Requires:** Docker + Docker Compose. Nothing else.

```bash
git clone <repo-url> && cd setu-recon
docker compose up
```

That's it. This starts MySQL, applies the schema, loads all 10,355 sample events,
and serves the API.

- API: <http://localhost:8000>
- **Interactive docs: <http://localhost:8000/docs>** ← start here
- Readiness + row counts: <http://localhost:8000/health/ready>

Confirm it's loaded:

```bash
curl -s localhost:8000/health/ready
# {"status":"ready","counts":{"events":10165,"transactions":3800,"merchants":5}}
```

`events: 10165` from a 10,355-event file is idempotency working: 190 duplicates absorbed.

### Running without Docker

```bash
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"
export DATABASE_URL="mysql+pymysql://user:pass@localhost:3306/setu"
python -m scripts.load_sample_data
uvicorn app.main:app --reload
```

### Sample data

The provided `sample_events.json` is used **unmodified** — 10,355 events, 5
merchants, 3,800 transactions, spanning 2026-01-08 to 2026-04-08.

After deduplication every transaction falls into exactly six shapes:

| post-dedup sequence | txns | meaning |
|---|---|---|
| initiated → processed → settled | 2,470 | healthy |
| initiated → failed | 570 | clean failure |
| initiated → processed | 380 | **processed, never settled** |
| initiated | 190 | **stuck** |
| initiated → processed → settled → settled | 95 | **double settlement** |
| initiated → failed → settled | 95 | **settled a failed payment** |

Verified properties: no transaction ever changes merchant or amount across its
events; no `settled` is ever timestamped before its `payment_processed`. The data is
internally consistent — the discrepancies are semantic, not corrupt.

---

## API documentation

Full interactive spec at **`/docs`** (OpenAPI, generated from the code — it cannot
drift from the implementation). Summary:

### `POST /events`

Accepts a single event object **or** an array. Idempotent by `event_id`.

```bash
curl -X POST localhost:8000/events -H 'content-type: application/json' -d '{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}'
```

```json
{ "received": 1, "ingested": 1, "duplicates": 0,
  "results": [{ "event_id": "b768e3a7-...", "status": "ingested",
                "transaction_id": "2f86e94c-..." }] }
```

Send it again → `"status": "duplicate"`, `"ingested": 0`, **HTTP 200**.

**Why 200 and not 409 for a duplicate?** The caller's intent — "this event should be
on record" — is satisfied either way. Webhook senders retry on timeouts and 5xx; a
409 would tell a correctly-behaving client it did something wrong, and could trigger
alerts or dead-lettering for a *successful* retry. Idempotency exists precisely so
retries are safe. Signalling an error on a safe retry defeats it. The per-event
`status` field still lets a caller observe the dedup.

| status | when |
|---|---|
| 200 | accepted (including duplicates) |
| 413 | batch exceeds `max_batch_size` (1000) |
| 422 | validation failure |

### `GET /transactions`

| param | notes |
|---|---|
| `merchant_id` | exact match |
| `status` | `pending` / `processed` / `failed` / `settled` (flattened) |
| `payment_status` | `pending` / `processed` / `failed` |
| `settlement_status` | `unsettled` / `settled` |
| `date_from` / `date_to` | ISO 8601, on `initiated_at`. `date_to` **exclusive** |
| `sort_by` | `initiated_at`, `processed_at`, `settled_at`, `last_event_at`, `amount` |
| `sort_dir` | `asc` / `desc` |
| `limit` / `offset` | limit capped at 100 |

The two axes combine to express what the flattened one cannot:

```bash
# the 95 transactions where money moved for a FAILED payment
curl "localhost:8000/transactions?payment_status=failed&settlement_status=settled"
```

`date_to` is exclusive to avoid the classic `BETWEEN` bug of silently dropping
everything timestamped after `00:00:00.000` on the final day.

### `GET /transactions/{transaction_id}`

Returns state, merchant, discrepancies, and the **complete event history**
oldest-first — including duplicates and superseded events. That history is the audit
trail: it's what lets an operator answer *why* a transaction is in this state.

### `GET /reconciliation/summary`

`?group_by=merchant`, `merchant,date`, `date,status`, … — any combination of
`merchant`, `date`, `status`, `payment_status`, `settlement_status`.

Per group: counts, plus **value reconciliation**:

| field | meaning |
|---|---|
| `expected_settlement_amount` | money that should have settled |
| `settled_amount` | money that did settle |
| `unreconciled_amount` | the gap ops has to chase |

**`unreconciled_amount` can be negative, and that is meaningful.** A settlement
against a failed payment adds to `settled_amount` but never to `expected` — so money
moving that shouldn't have shows up as a negative gap. It is a signal, not a bug.

The summary and the discrepancy endpoint are independent SQL, and they agree exactly:

```
unreconciled  =  processed_never_settled  −  settled_despite_failure
₹7,537,522.50 =  ₹9,636,198.03            −  ₹2,098,675.53    ✓ to the paisa
```

### `GET /reconciliation/discrepancies`

`?type=`, `?merchant_id=`, `?date_from=`, `?date_to=`, `?as_of=`, `?sla_hours=`,
`?limit=`, `?offset=`.

Returns `counts_by_type` plus the paginated rows. A transaction can breach several
rules, so `counts_by_type` may sum to more than `pagination.total`.

### `GET /reconciliation/rules`

The live SQL predicate for each rule. See [Discrepancy rules](#discrepancy-rules).

### Errors

Every failure returns one envelope, so a client writes one error handler:

```json
{ "error": { "code": "validation_error", "message": "...", "field": "amount" } }
```

Database errors are logged in full and returned generically — driver messages quote
row values and schema internals, which leaks data and hands an attacker a free
schema map.

---

## Deployment

_TBD — see [Tradeoffs](#tradeoffs-and-what-id-do-differently)._

---

## Testing

```bash
docker compose run --rm api pytest -v
```

Covers: idempotent replay, out-of-order arrival, the discrepancy truth table against
known sample-data counts, pagination stability, and validation rejection.

CI runs the suite against a real MySQL on every push (`.github/workflows/ci.yml`).

---

## Assumptions

1. **`event_id` is globally unique and stable across retries.** The entire
   idempotency guarantee rests on this. If a sender generated a fresh `event_id` per
   retry, no receiver could distinguish a retry from a real second event.
2. **All events for one transaction agree on `merchant_id`, `amount` and `currency`.**
   Verified true across all 10,355 sample events. First observation wins; these are
   treated as invariants, not merge targets.
3. **Naive timestamps are UTC.** Sample data is all timezone-aware; a naive value is
   assumed UTC rather than rejected, since rejecting mid-integration is a worse
   failure mode than a documented assumption.
4. **`payment_failed` is terminal and beats `payment_processed`** if both were ever
   seen. Conservative: don't claim money is good. No such row exists in the sample.
5. **The date dimension is the UTC date of `payment_initiated`** — the transaction's
   business date.
6. **A single SLA governs both time-relative rules.** In reality, processing and
   settlement would have different SLAs; both sample cohorts are far past 24h either
   way.
7. **No authentication.** Out of scope for the assignment. In production this
   endpoint would need at minimum a shared secret or HMAC signature verification —
   an unauthenticated ingestion endpoint lets anyone forge settlements.

---

## Tradeoffs and what I'd do differently

### Why MySQL, and what it cost

Chosen for familiarity and because it's a defensible production choice. Real costs
versus PostgreSQL:

- **No `RETURNING`** — drove the recompute design. This turned out to be a *better*
  design (simpler, self-evidently correct), so the constraint helped.
- **No partial indexes.** Postgres can index *only the broken rows*
  (`WHERE processed_at IS NOT NULL AND first_settled_at IS NULL` — ~380 of 3,800
  rows), making discrepancy queries near-instant off a tiny index. MySQL has no
  equivalent. Irrelevant at 3,800 rows; at millions I'd need a materialised
  discrepancy table refreshed on a schedule, which reintroduces staleness.
- **No `FILTER`** — conditional aggregates use `COUNT(CASE WHEN … THEN 1 END)`.
  Same result, noisier.
- **No timezone-aware timestamp** — see `DATETIME(6)` above.

### Offset pagination, not keyset

Keyset (`WHERE (sort_key, id) < (…)`) is O(1) at any depth. Offset degrades — `OFFSET
100000` makes MySQL walk and discard 100,000 rows. But keyset can't do random page
access or total counts, and gets ugly with user-selectable sort columns.

Ops tooling wants *"page 4 of 76, 3,800 results"*. At 3,800 rows offset is free.
`count(*) OVER ()` has the same caveat: it counts all matching rows. Past ~100k rows
I'd move to keyset and drop exact totals. **Naming the limit beats silently
over-building for scale that doesn't exist.**

### Index tuning — measured, and two were removed

Every index was benchmarked before being kept. Indexes are not free: each one is
extra work on the hot ingestion path, and this service writes far more than it reads.

| index | measured at 500k rows | kept? |
|---|---|---|
| `(merchant_id, initiated_at DESC)` | **16×** faster (0.75ms vs 11.9ms) at 2,000 merchants | ✓ |
| `(initiated_at DESC)` | the workhorse — planner's default choice | ✓ |
| `(status, initiated_at DESC)` | **slower** than the plan already chosen | ✗ removed |
| `(merchant_id, status, initiated_at DESC)` | 1.7× on an already-sub-1ms query | ✗ removed |

The merchant composite looks useless against the sample's **5 merchants** — a
1-of-5 filter isn't selective, so the planner correctly ignores it. It's sized for
production merchant cardinality, where the logic inverts. That's why it was
benchmarked at 2,000 merchants rather than 5.

### `CHAR(36)` for UUIDs, not `BINARY(16)`

`BINARY(16)` is 20 bytes smaller per row and faster to index. `CHAR(36)` is
human-readable in `psql`/Workbench and needs no `UUID_TO_BIN`/`BIN_TO_UUID` noise on
every query. At this scale readability wins; a reconciliation tool is one people read
by hand. At tens of millions of rows I'd switch.

### Not done, deliberately

- **Amount-mismatch detection.** The classic real-world reconciliation discrepancy —
  settled amount ≠ processed amount (partial settlement, fees deducted). The sample
  data has none (verified), so it would be untested code against imaginary data.
  Would be a sixth rule.
- **Alembic migrations.** A single greenfield schema doesn't need migration tooling.
  `sql/schema.sql` is idempotent DDL applied at startup, so a fresh container is
  self-provisioning. A schema with real history would need Alembic.
- **Authentication.** See Assumptions.
- **Rate limiting / backpressure.** A real ingestion endpoint needs both.

### With more time

1. **Amount-mismatch rule** with generated test data.
2. **A `POST /admin/rebuild-projection` endpoint.** The projection is rebuildable by
   design; that capability should be exercised in a test rather than merely claimed.
3. **Materialised summary rollups.** `GROUP BY` over the full table is fine at 3,800
   rows and won't be at 50M. Daily rollups per merchant, incrementally maintained.
4. **Structured logging + request IDs.** Currently plain log lines.

---

## AI tool disclosure

**Tool used: Claude (Anthropic), via Claude Code.**

**How.** I used it as a pair-programmer and a tutor. Concretely:

- **Data analysis.** Profiling `sample_events.json` to find the six post-dedup
  transaction shapes, the 190 exact duplicates and their split across event types,
  and the settlement-lag distribution that produced the 24h SLA. This directly shaped
  the schema.
- **Design discussion.** Working through the alternatives for each decision —
  event-sourcing vs projection, one status column vs two axes, incremental merge vs
  recompute, materialised vs read-time discrepancies — with the reasoning for each.
- **Code generation**, reviewed and revised by me.
- **Benchmarking.** Generating the 500k-row test tables that produced the index
  numbers above, including the evidence for removing two indexes.
- **Explanation.** I'm stronger in Python than in SQL and database design. I used it
  heavily to understand rather than just to produce — generated columns, why a
  primary key beats a check-then-insert, why `MIN(CASE WHEN …)` yields `NULL` for a
  milestone that never happened. Every design decision in this README is one I can
  explain and defend, because I made sure I understood it before keeping it.

**What I decided.** MySQL over PostgreSQL (familiarity, and I wanted a stack I could
defend). Recompute over incremental merge (simplicity over cleverness). Removing the
two indexes that didn't earn their write cost. Treating `stuck_pending` as a
reportable discrepancy rather than merely "not yet resolved".

# SETU
