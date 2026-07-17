# Reconciliation

Five discrepancy classes. Each is a `WHERE` clause over a **single row** — no joins, no
aggregation, no scanning event history. The schema did the work.

Live SQL for every rule: [`/reconciliation/rules`](https://setu-recon.onrender.com/reconciliation/rules).
Implementation: [`app/queries.py`](../app/queries.py).

## The rules

| type | rule | count |
|---|---|---|
| `processed_never_settled` | processed, unsettled, past SLA | **380** |
| `settled_despite_failure` | `failed_at` **and** `first_settled_at` both set | **95** |
| `duplicate_settlement` | `settled_event_count > 1` | **95** |
| `stuck_pending` | initiated only, past SLA | **190** |
| `settled_without_processing` | settled with no prior processing | **0** |

```sql
-- settled_despite_failure. One line. The whole "settlement recorded for a
-- failed payment" requirement.
(t.failed_at IS NOT NULL AND t.first_settled_at IS NOT NULL)
```

That line is only *writable* because payment and settlement are independent axes. With
one combined `status` column the row would have already forgotten it failed. Every
schema decision existed to make these five lines possible.

A transaction can breach several rules, so `counts_by_type` may sum to more than
`pagination.total`.

### `duplicate_settlement` counts events, not rows

A settlement replayed under the same `event_id` never reaches the counter — `INSERT
IGNORE` drops it before the recompute runs. **That's the difference between 162 and 95.**
See [Idempotency](idempotency.md).

### Why `settled_without_processing` exists at zero

It's the canary. Settlement without processing is impossible if events are honest — so a
non-zero count means *our ingestion* is broken, not the merchant's money. It's also
exactly the corruption an order-dependent state machine would manufacture from
out-of-order events.

It costs one line, and it's the difference between "we found no problems" and "we looked
for this specific problem and there were none."

### Why `stuck_pending` is reported

Not strictly a payment/settlement contradiction — a pending payment with no settlement is
*consistent*, just unresolved. But a payment initiated three months ago that never
resolved is an operational problem someone must chase, so it's reported.

## Computed at read time, not stored

Two rules are **time-relative**: a transaction becomes a discrepancy purely by getting
**older**. No event arrives. Nothing triggers a write.

A stored `is_broken` flag would be wrong the instant the clock crossed the SLA, unless a
cron job re-swept the table forever. Computing on read means the answer is always correct
as of the moment you ask.

```
cutoff = as_of − sla_hours       (default: now − 24h)
```

`?as_of=` also makes reports reproducible — an ops team reconciling "as of yesterday
23:59" can re-run it next week and get the same numbers — and makes the rules testable
without sleeping for a day.

## Where 24h came from

Measured from the sample data, not guessed:

- Every settlement lands within **6.00h** of processing (p50 2.93h, p99 5.95h — a hard ceiling)
- Every never-settled transaction is **≥17.3h** old

There's a clean, wide gap between "normal" and "broken". 24h sits inside it with 4×
margin over observed p99, and matches the **T+1** settlement convention in Indian
payments. It's a query parameter, so ops can tighten it without a deploy.

## Value reconciliation

The summary reports money, not just counts:

| field | meaning |
|---|---|
| `expected_settlement_amount` | money that **should** have settled (processed payments) |
| `settled_amount` | money that **did** settle |
| `unreconciled_amount` | `expected − settled` — the gap to chase |

A row count tells an ops person nothing actionable. *"₹1.5M of UrbanEats money processed
but hasn't landed"* is a thing they can act on today.

**`unreconciled_amount` can go negative, and that's meaningful.** A settlement against a
failed payment adds to `settled_amount` but never to `expected` — so money moving that
shouldn't have shows up as a negative gap. A signal, not a bug. Don't `ABS()` it.

### It cross-checks itself

The summary and the discrepancy endpoint are computed by completely independent SQL, and
they agree exactly:

```
unreconciled  =  processed_never_settled  −  settled_despite_failure
₹7,537,522.50 =  ₹9,636,198.03            −  ₹2,098,675.53      ✓ to the paisa
```

## Query design

Everything is one `GROUP BY` with `CASE` aggregates — a single pass over the rows. The
alternative (one subquery per metric) scans the table once per metric.

```sql
COUNT(CASE WHEN t.payment_status = 'processed' THEN 1 END)
```

MySQL has no `FILTER (WHERE ...)`, so the `CASE` turns non-matching rows into `NULL` and
`COUNT`/`SUM` ignore `NULL`. Same result as Postgres's `FILTER`, noisier.

Dimensions and sort fields resolve through **whitelist dicts** — `GROUP BY` and `ORDER
BY` take identifiers, which cannot be bound as parameters, so user input is used to *look
up* SQL we wrote rather than to *become* SQL. Everywhere else, values are bound
parameters sent to MySQL on a separate channel from the query text.
