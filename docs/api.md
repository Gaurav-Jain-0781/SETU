# API reference

Full interactive spec: [`/docs`](https://setu-recon.onrender.com/docs) — generated from
the code, so it cannot drift from the implementation.

## `POST /events`

Accepts a single event object **or** an array. Idempotent by `event_id`.

One endpoint serves both the partner's live feed and their replay/backfill path — they
differ only in cardinality.

```bash
curl -X POST https://setu-recon.onrender.com/events \
  -H 'content-type: application/json' -d '{
    "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
    "event_type": "payment_initiated",
    "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
    "merchant_id": "merchant_2",
    "merchant_name": "FreshBasket",
    "amount": 15248.29,
    "currency": "INR",
    "timestamp": "2026-01-08T12:11:58.085567+00:00"}'
```

```json
{ "received": 1, "ingested": 1, "duplicates": 0,
  "results": [{ "event_id": "b768e3a7-...", "status": "ingested",
                "transaction_id": "2f86e94c-..." }] }
```

Send it again → `"status": "duplicate"`, `"ingested": 0`, **HTTP 200**.

**Why 200 and not 409?** The caller's intent — "this event should be on record" — is
satisfied either way. Webhook senders retry on timeouts and 5xx; a 409 would tell a
correctly-behaving client it failed, and could trigger alerts for a *successful* retry.
Idempotency exists so retries are safe. The per-event `status` still exposes the dedup.

Results are reported **per submitted position**, not per distinct `event_id` — so a batch
containing the same event twice reads `ingested, duplicate`, and `received` always equals
`len(results)`.

| status | when |
|---|---|
| 200 | accepted (including duplicates) |
| 413 | batch exceeds `max_batch_size` (1000) |
| 422 | validation failure |

## `GET /transactions`

| param | notes |
|---|---|
| `merchant_id` | exact match |
| `status` | `pending` / `processed` / `failed` / `settled` — flattened convenience axis |
| `payment_status` | `pending` / `processed` / `failed` |
| `settlement_status` | `unsettled` / `settled` |
| `date_from` / `date_to` | ISO 8601, on `initiated_at`. `date_to` **exclusive** |
| `sort_by` | `initiated_at`, `processed_at`, `settled_at`, `last_event_at`, `amount` |
| `sort_dir` | `asc` / `desc` |
| `limit` / `offset` | limit capped at 100 |

The two real axes combine to express what the flattened one cannot:

```bash
# the 95 transactions where money moved for a FAILED payment
curl "https://setu-recon.onrender.com/transactions?payment_status=failed&settlement_status=settled"
```

**Use `Z`, not `+00:00`, in date params.** In a URL query string `+` means *space*, so
`2026-02-01T00:00:00+00:00` arrives malformed unless percent-encoded. `Z` is the same
instant with no special characters.

`date_to` is exclusive, which avoids the classic `BETWEEN` bug of silently dropping
everything timestamped after `00:00:00.000` on the final day.

`ORDER BY` always carries `transaction_id` as a tie-breaker. Without a total order,
tied sort values may be permuted differently per query — under OFFSET pagination that
shows one row on two pages and silently skips another.

`total` comes from `COUNT(*) OVER ()`, evaluated after `WHERE` but before `LIMIT`, so one
round trip returns both the page and the full count. A second `COUNT` query would double
the round trips and could disagree with the page under concurrent writes.

## `GET /transactions/{transaction_id}`

Returns state, merchant, discrepancies, and the **complete event history** oldest-first —
including duplicates and superseded events.

That history is the audit trail: it's what lets an operator answer *why* a transaction is
in a given state, which is the entire reason `payment_events` is append-only.

## `GET /reconciliation/summary`

`?group_by=merchant`, `merchant,date`, `date,status`, … — any combination of `merchant`,
`date`, `status`, `payment_status`, `settlement_status`. Also `merchant_id`, `date_from`,
`date_to`.

Per group: counts plus value reconciliation (`expected_settlement_amount`,
`settled_amount`, `unreconciled_amount`). See [Reconciliation](reconciliation.md).

## `GET /reconciliation/discrepancies`

`?type=`, `?merchant_id=`, `?date_from=`, `?date_to=`, `?as_of=`, `?sla_hours=`,
`?limit=`, `?offset=`.

Returns `counts_by_type` plus the paginated rows. A transaction can breach several rules,
so `counts_by_type` may sum to more than `pagination.total`.

## `GET /reconciliation/rules`

The live SQL predicate for each rule — the same strings the queries are built from, not a
prose paraphrase that could drift. Reconciliation output is only trustworthy if an
operator can audit what the service means by "broken".

## `GET /health` and `/health/ready`

`/health` deliberately does **not** touch the database. A liveness probe that queried
MySQL would report the app as dead during a transient blip and invite a restart of a
process that's fine. `/health/ready` checks the DB and returns row counts, so you can
confirm the data is loaded rather than finding an empty database behind a healthy service.

## Errors

Every failure returns one envelope, so a client writes one error handler:

```json
{ "error": { "code": "validation_error", "message": "...", "field": "amount" } }
```

| code | status |
|---|---|
| `validation_error` | 422 |
| `not_found` | 404 |
| `payload_too_large` | 413 |
| `conflict` / `integrity_error` | 409 |
| `database_unavailable` | 503 |

Database errors are logged in full and returned generically — driver messages quote row
values and schema internals, which leaks data and hands an attacker a free schema map.

Validation is strict: unknown fields are rejected (`extra="forbid"`). Silently ignoring a
typo'd `ammount` would mean accepting an event whose amount we never read, and
reconciling money we never received.
