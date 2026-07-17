# Idempotency

> *"Submitting the same event twice should not create duplicate records or corrupt
> transaction state."*

Implementation: [`app/ingest.py`](../app/ingest.py).

## The mechanism is a primary key

```sql
PRIMARY KEY (event_id)
```

Not application code. This is broken:

```python
if not event_exists(event_id):   # two concurrent replays both pass this
    insert(event)                # ...and both land here
```

The check and the write are separate steps, and the world can change between them. Two
copies arriving at once both see "doesn't exist" and both insert. No amount of care
closes that window.

A primary key makes checking and writing **the same operation**. `INSERT IGNORE` means
the database decides, atomically. There is no in-between to race through.

## The harder half: not double-counting

Storing the event once is easy. The trap is step two — updating `transactions` *only if*
the event was genuinely new. A replayed `settled` that bumps `settled_event_count`
invents a settlement that never happened.

PostgreSQL solves this with `RETURNING` (insert, skip duplicates, hand back which were
new). **MySQL has no `RETURNING`.**

## So we never ask

```
1. INSERT IGNORE the events.
   Duplicates silently dropped. The log is now correct — the primary key guarantees it.

2. RECOMPUTE each touched transaction by reading ALL its events back out of the log.
```

Step 2 doesn't care whether the events were new. It reads the deduplicated log and
derives the answer. Fresh or replayed, the result is identical.

```sql
settled_event_count = COUNT(settled rows in the log)   -- recount: can't be wrong
settled_event_count = settled_event_count + 1          -- tally: wrong on replay
```

A recount doesn't add to anything — it overwrites with a fresh answer off the log. Run
it once or fifty times, same result.

**Two guarantees, chained:**

```
PRIMARY KEY (event_id)  →  the log holds each event exactly once
                            ↓
recompute from the log  →  the projection is always correct
```

## What this buys for free

**Out-of-order events self-heal.** A `settled` arriving before its `payment_processed`
just sets `first_settled_at`; when `processed` arrives, the next recompute sees both. No
state machine, no "illegal transition" to reject.

**Unknown transactions are acceptable.** A `settled` for a transaction we've never seen
creates the row rather than 404-ing. Across uncoordinated systems you cannot assume the
initiator's events reach you first.

**Crash safety.** The `INSERT IGNORE` and the recompute run in **one DB transaction**
(`ENGINE=InnoDB`). Either both commit or both roll back — the half-state where the log
has an event the projection doesn't know about cannot be observed.

**Recoverability.** Because `transactions` is derived, it can be dropped and rebuilt from
the log — the same recompute, minus its `WHERE`. A design that increments counters can
never do this: the increments are gone and the current number is all you have. Once
wrong, wrong forever. (`rebuild_projection()`, and a test that proves it.)

## Why this beats the cleverer alternative

An incremental merge (`LEAST`/`GREATEST`/`+`) is O(1) per event and never reads events
back. Faster in theory. But:

| | incremental merge | recompute from log |
|---|---|---|
| Cost | O(1) | reads ~2.7 rows (10,165 ÷ 3,800) |
| Needs `RETURNING` | yes | **no** |
| Correct if the reasoning is subtly wrong | no | **yes — it re-derives from truth** |
| Explainable in one sentence | not really | *"recompute from the log"* |

**Chosen deliberately.** It trades a theoretical performance win for a design whose
correctness is self-evident rather than argued. The clever version is correct *only if*
my reasoning about commutative operators holds. The simple version is correct because it
reads the truth and reports it.

## The 162-vs-95 trap

The sample data contains **190 exact-duplicate `event_id`s** with byte-identical
payloads — 64 `initiated`, 59 `processed`, **67 `settled`**.

Those 67 matter enormously:

> **Naive ingestion reports 162 double-settlements. Only 95 are real.**

The other 67 are the same settlement delivered twice. Idempotency isn't hygiene here —
get it wrong and the reconciliation report is inflated 70%, and an ops team chases 67
problems that don't exist.

## Verified, not asserted

Replaying all 10,355 events against a loaded database:

```
received:   10,355
ingested:        0
duplicates: 10,355
```

An MD5 over **every column of all 3,800 transaction rows** is byte-identical before and
after. Not "approximately unchanged" — bit-for-bit. `updated_at` doesn't move either,
because with zero new events the recompute has nothing to write.

Live, against production:

```bash
curl -X POST https://setu-recon.onrender.com/events \
  -H 'content-type: application/json' -d '{
    "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
    "event_type": "payment_initiated",
    "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
    "merchant_id": "merchant_2", "merchant_name": "FreshBasket",
    "amount": 15248.29, "currency": "INR",
    "timestamp": "2026-01-08T12:11:58.085567+00:00"}'
# {"received":1,"ingested":0,"duplicates":1, ...}
```

Run it a hundred times; the event count stays at 10,165.

## Why a duplicate returns 200, not 409

The caller's intent — "this event should be on record" — is satisfied either way. Webhook
senders retry on timeouts and 5xx; a `409` would tell a correctly-behaving client it did
something wrong, and could trigger alerts or dead-lettering for a *successful* retry.
Idempotency exists precisely so retries are safe. Erroring on a safe retry defeats it.

The per-event `status` field still lets a caller observe the dedup.
