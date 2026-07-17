"""Idempotency — the assignment's non-negotiable.

These are the most important tests in the suite. Every other property of the
service can be wrong and be caught by inspection; a double-counted settlement is
silent, plausible, and reports a merchant's money as broken when it isn't.
"""

from sqlalchemy import text

from tests.factory import event, happy_path, new_txn


def test_same_event_twice_creates_one_row(client, db):
    """The base case: replaying an event must not duplicate it."""
    txn = new_txn()
    e = event("payment_initiated", txn)

    first = client.post("/events", json=e).json()
    second = client.post("/events", json=e).json()

    assert first["ingested"] == 1
    assert first["results"][0]["status"] == "ingested"

    # A duplicate is a SUCCESS, not an error. A webhook sender retrying after a
    # lost 200 did nothing wrong; answering 4xx would tell a correct client it
    # failed. See README § Idempotency.
    assert second["ingested"] == 0
    assert second["duplicates"] == 1
    assert second["results"][0]["status"] == "duplicate"

    count = db.execute(
        text("SELECT COUNT(*) FROM payment_events WHERE event_id = :id"),
        {"id": e["event_id"]},
    ).scalar_one()
    assert count == 1


def test_replayed_settlement_does_not_inflate_count(client, db):
    """THE test. A replayed `settled` must not look like a second settlement.

    This is the exact bug that turns the sample data's 95 real double-settlements
    into a phantom 162 — a 70% inflated reconciliation report, and 67 merchants
    chased over money that settled exactly once.

    It is the reason ingestion recomputes from the log instead of incrementing.
    """
    txn = new_txn()
    settled = event("settled", txn, minutes=180)

    client.post("/events", json=event("payment_initiated", txn, minutes=0))
    client.post("/events", json=event("payment_processed", txn, minutes=5))
    client.post("/events", json=settled)

    def settled_count() -> int:
        return db.execute(
            text("SELECT settled_event_count FROM transactions WHERE transaction_id = :t"),
            {"t": txn},
        ).scalar_one()

    assert settled_count() == 1

    # Replay the same settlement ten times. A counter would now read 11.
    for _ in range(10):
        client.post("/events", json=settled)

    assert settled_count() == 1, "replayed settlement was counted as a real one"

    detail = client.get(f"/transactions/{txn}").json()
    assert detail["discrepancies"] == [], "a replay was misreported as a duplicate settlement"


def test_distinct_settlement_events_are_counted(client, db):
    """The other half: don't over-correct and hide REAL duplicate settlements.

    Two settlements with different event_ids are two genuine settlement events —
    exactly the 95 in the sample data. An implementation that deduplicated on
    (transaction_id, event_type) instead of event_id would pass the test above and
    fail this one, silently hiding real financial errors.
    """
    txn = new_txn()
    client.post("/events", json=event("payment_initiated", txn, minutes=0))
    client.post("/events", json=event("payment_processed", txn, minutes=5))
    client.post("/events", json=event("settled", txn, minutes=180))
    client.post("/events", json=event("settled", txn, minutes=184))  # different event_id

    count = db.execute(
        text("SELECT settled_event_count FROM transactions WHERE transaction_id = :t"),
        {"t": txn},
    ).scalar_one()
    assert count == 2

    detail = client.get(f"/transactions/{txn}").json()
    assert "duplicate_settlement" in detail["discrepancies"]


def test_duplicate_within_a_single_batch(client, db):
    """Duplicates arriving in ONE request, not two.

    A different code path from two separate calls — this is deduplicated inside a
    single INSERT IGNORE rather than against previously committed rows. The sample
    file contains 190 such pairs, so this is the path the loader actually takes.
    """
    txn = new_txn()
    e = event("settled", txn, minutes=180)
    body = [event("payment_initiated", txn, minutes=0), e, e]  # e twice

    resp = client.post("/events", json=body).json()

    assert resp["received"] == 3
    assert resp["ingested"] == 2
    assert resp["duplicates"] == 1
    # Reported per submitted position: first occurrence ingested, second duplicate.
    assert [r["status"] for r in resp["results"]] == ["ingested", "ingested", "duplicate"]

    assert (
        db.execute(
            text("SELECT settled_event_count FROM transactions WHERE transaction_id = :t"),
            {"t": txn},
        ).scalar_one()
        == 1
    )


def test_full_replay_leaves_state_byte_identical(client, db):
    """Replaying an entire history changes nothing at all.

    The strongest statement of idempotency available: not "roughly unchanged", but
    every column of every row identical. This mirrors the check run against the
    real 10,355-event file (see README).
    """
    txns = [new_txn() for _ in range(5)]
    events = [e for t in txns for e in happy_path(t)]
    client.post("/events", json=events)

    def fingerprint() -> str:
        return db.execute(
            text("""
                SELECT MD5(GROUP_CONCAT(
                    CONCAT_WS('|', transaction_id, merchant_id, amount, currency,
                        COALESCE(initiated_at,''), COALESCE(processed_at,''),
                        COALESCE(failed_at,''), COALESCE(first_settled_at,''),
                        COALESCE(last_settled_at,''), settled_event_count,
                        event_count, status)
                    ORDER BY transaction_id SEPARATOR '#'))
                FROM transactions
            """)
        ).scalar_one()

    before = fingerprint()
    resp = client.post("/events", json=events).json()
    after = fingerprint()

    assert resp["ingested"] == 0
    assert resp["duplicates"] == len(events)
    assert before == after


def test_projection_is_rebuildable_from_the_log(client, db):
    """`transactions` holds no information the event log doesn't.

    The projection is derived, and a derived thing should be provably
    re-derivable rather than merely claimed to be. This is what makes the design
    recoverable: corrupt the projection and you lose nothing permanent.
    """
    from app.ingest import rebuild_projection

    txns = [new_txn() for _ in range(3)]
    client.post("/events", json=[e for t in txns for e in happy_path(t)])

    def snapshot() -> list[tuple]:
        return [
            tuple(r)
            for r in db.execute(
                text("""
                    SELECT transaction_id, initiated_at, processed_at, failed_at,
                           first_settled_at, settled_event_count, event_count, status
                    FROM transactions ORDER BY transaction_id
                """)
            ).fetchall()
        ]

    before = snapshot()

    with db.engine.begin() as conn:
        rebuilt = rebuild_projection(conn)

    assert rebuilt == len(txns)
    assert snapshot() == before, "rebuild from the log produced a different projection"
