# Setu Reconciliation Service

Ingests payment lifecycle events, maintains transaction state, and reports where
payment state and settlement state disagree.

**Live:** <https://setu-recon.onrender.com/docs> — interactive docs, click "Try it out".
*First request takes ~30–50s (free tier sleeps). Not broken; retry once.*

```bash
curl https://setu-recon.onrender.com/health/ready
# {"status":"ready","counts":{"events":10165,"transactions":3800,"merchants":5}}
```

**10,165 events stored from a 10,355-event file.** The 190 missing are duplicates the
database refused. That gap is the whole design.

---

## Run it locally

Needs Docker. Nothing else.

```bash
docker compose up
```

Starts MySQL, applies the schema, loads all 10,355 sample events, serves on
<http://localhost:8000> — docs at `/docs`.

---

## Docs

| | |
|---|---|
| [Data model](docs/data-model.md) | Three tables, why status is two columns, why generated |
| [Idempotency](docs/idempotency.md) | The mechanism, the 162-vs-95 trap, how it's verified |
| [Reconciliation](docs/reconciliation.md) | The 5 rules, where the 24h SLA came from |
| [API](docs/api.md) | Endpoints, parameters, error contract |
| [Deployment](docs/deployment.md) | TiDB + Render, and a TiDB gotcha worth knowing |
| [Testing](docs/testing.md) | What's covered and why |
| [Tradeoffs](docs/tradeoffs.md) | What this cost, index benchmarks, assumptions |

---

## Architecture

**We are a receiver, not a sender.** A partner gets payment events from several systems
— Setu's rails, banks, their own gateway — and this service ingests them.

```
Setu's system  ──┐
Bank / NPCI    ──┼──►  POST /events ──►  payment_events   append-only. THE TRUTH.
Partner gateway──┘                              │
                                                │ recompute (same DB transaction)
                                                ▼
                                        transactions      derived projection
                                                │
                                                ▼  read-time SQL
                              GET /transactions · /reconciliation/*  ──► ops team
```

Two consequences drive every decision:

**Duplicates are structural, not a bug.** A sender delivers an event, our `200` is lost
to a network blip, and it retries — correctly, because it cannot know we got it. No
upstream fix exists. The sample data has 190.

**Order is not guaranteed.** Those systems don't coordinate. A `settled` can arrive
before its `payment_processed`. Any design assuming order will corrupt itself.

**Events are truth; `transactions` is a cache of conclusions.** Storing only state
destroys history; storing only events means every query re-derives 3,800 transactions
from 10,165 events. Storing both pays once per event (~10k times) instead of once per
question (constantly) — and because the projection is *derived*, it can be rebuilt from
the log if it's ever wrong.

```
app/
  ingest.py     idempotent ingestion  ← the heart
  queries.py    every read query, in one auditable file
  schemas.py    Pydantic validation + response contracts
  routers/      one module per endpoint group
sql/schema.sql  hand-written DDL + indexes, rationale inline
tests/          46 tests — MySQL and TiDB
```

**No ORM.** It would hide the schema, indexes and queries — the things most worth
reviewing. All filtering, aggregation, sorting and pagination happen in SQL; Python
never receives a row it intends to discard, and never sums a column.

→ [Data model](docs/data-model.md)

---

## Engineering decisions

- Events are the source of truth; `transactions` is a derived projection, rebuildable from the log.
- Recompute that projection rather than increment it — a recount off a deduplicated log can't double-count.
- Payment and settlement are separate columns; one `status` enum can't express "failed but settled".
- Reconciliation computed on read, not stored — two rules are time-relative, so a saved flag goes stale.
- Idempotency enforced by a primary key on `event_id`, not by application code that could race.
- Used MySQL to satisfy the SQL requirement while leveraging existing expertise.
- Kept the service stateless to enable horizontal scaling.
- Used FastAPI because automatic OpenAPI documentation aligns well with API-first integrations.

---

## What it finds

| type | count | meaning |
|---|---|---|
| `processed_never_settled` | **380** | payment succeeded, money never moved |
| `stuck_pending` | **190** | initiated, then nothing, ever |
| `settled_despite_failure` | **95** | money moved for a payment that failed |
| `duplicate_settlement` | **95** | settled twice by distinct events |
| `settled_without_processing` | **0** | the canary — impossible if upstream is honest |

**`duplicate_settlement` is 95, not 162.** Naive ingestion reports 162 — the other 67
are the same settlement delivered twice. Idempotency isn't hygiene here: get it wrong
and the report is inflated 70%, and an ops team chases 67 problems that don't exist.

→ [Idempotency](docs/idempotency.md) · [Reconciliation](docs/reconciliation.md)

---

## API

Full interactive spec at [`/docs`](https://setu-recon.onrender.com/docs) — generated from
the code, so it can't drift.

| endpoint | |
|---|---|
| `POST /events` | Single object **or** array. Idempotent by `event_id`. A duplicate returns `200`, not an error. |
| `GET /transactions` | Filter by merchant, status, both status axes, date range. Sort, paginate. |
| `GET /transactions/{id}` | State + merchant + discrepancies + full event history |
| `GET /reconciliation/summary` | Group by any of merchant/date/status, with value reconciliation |
| `GET /reconciliation/discrepancies` | The 5 rules. `?type=`, `?as_of=`, `?sla_hours=` |
| `GET /reconciliation/rules` | The live SQL behind each rule |

→ [API reference](docs/api.md)

---

## Deployment

App on **Render** ([`render.yaml`](render.yaml)), database on **TiDB Cloud** — Render's
managed database is PostgreSQL-only. TiDB is free, speaks the MySQL wire protocol, and
is serverless so it can't power off between a reviewer's visits.

⚠️ **TiDB silently ignores `CHECK` constraints** unless
`tidb_enable_check_constraint = ON`. Caught by the test suite on TiDB before deploying.

→ [Deployment guide](docs/deployment.md)

---

## Testing

**46 tests, passing on both MySQL 8.4 and TiDB.**

```bash
docker compose up -d db && pytest -q
```

Also [`postman_collection.json`](postman_collection.json) — 26 requests, 55 assertions,
verified against the live deployment with newman.

→ [Testing](docs/testing.md)

---

## Assumptions and tradeoffs

Short version: **`event_id` is stable across retries** (the whole guarantee rests on it)
· **no authentication** (out of scope; production needs HMAC at minimum) · **MySQL cost
us `RETURNING` and partial indexes** — the first forced a simpler, better design · **two
indexes were benchmarked and removed** for not earning their write cost.

→ [Tradeoffs and assumptions](docs/tradeoffs.md)

---

## AI tool disclosure

**Claude (Anthropic), via Claude Code.** Used as a pair-programmer and a tutor:

- **Data analysis** — profiling `sample_events.json` to find the six post-dedup transaction shapes, the 190 duplicates and their split by event type, and the settlement-lag distribution behind the 24h SLA. This shaped the schema.
- **Design discussion** — alternatives for each decision (projection vs event-sourcing, one status vs two axes, recompute vs increment, read-time vs materialised discrepancies), with the reasoning.
- **Code generation**, reviewed and revised by me.
- **Benchmarking** — the 500k-row tables behind the index numbers, including the evidence for removing two.
- **Explanation.** I'm stronger in Python than in SQL and database design, so I used it heavily to *understand* rather than just produce. Every decision here is one I can defend, because I made sure I understood it before keeping it.

**My calls:** MySQL over PostgreSQL. Recompute over incremental merge (simplicity over
cleverness). Removing the two indexes that didn't pay for themselves. Treating
`stuck_pending` as a reportable discrepancy rather than merely "not yet resolved".
