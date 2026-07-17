"""The discrepancy truth table, and out-of-order tolerance.

Each rule gets a transaction built to trigger it and nothing else — so a test
failure names the broken rule rather than "something in reconciliation".
"""

from datetime import timedelta

import pytest

from tests.factory import BASE, event, happy_path, new_txn

# Far enough past BASE that every SLA-relative rule has fired.
AS_OF = (BASE + timedelta(days=30)).isoformat()


def discrepancies_for(client, txn: str) -> list[str]:
    return client.get(f"/transactions/{txn}", params={"as_of": AS_OF}).json()["discrepancies"]


# ---------------------------------------------------------------------------
# The five rules
# ---------------------------------------------------------------------------


def test_healthy_transaction_has_no_discrepancies(client):
    """The control. Without this, a rule that fires on everything would look fine."""
    txn = new_txn()
    client.post("/events", json=happy_path(txn))
    assert discrepancies_for(client, txn) == []


def test_clean_failure_is_not_a_discrepancy(client):
    """A failed payment that never settled is CORRECT, not broken.

    570 of the sample's transactions. Flagging these would bury the 190 real
    problems in noise.
    """
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0),
            event("payment_failed", txn, minutes=10),
        ],
    )
    assert discrepancies_for(client, txn) == []


def test_processed_never_settled(client):
    """Payment succeeded, money never moved. 380 in the sample data."""
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0),
            event("payment_processed", txn, minutes=5),
        ],
    )
    assert discrepancies_for(client, txn) == ["processed_never_settled"]


def test_settled_despite_failure(client):
    """Money moved for a payment that FAILED. 95 in the sample data.

    Only detectable because payment_status and settlement_status are independent
    axes. With one combined status column, `settled` would have overwritten
    `failed` and this transaction would be indistinguishable from a healthy one.
    """
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0),
            event("payment_failed", txn, minutes=10),
            event("settled", txn, minutes=200),
        ],
    )
    assert discrepancies_for(client, txn) == ["settled_despite_failure"]

    detail = client.get(f"/transactions/{txn}").json()
    assert detail["payment_status"] == "failed"
    assert detail["settlement_status"] == "settled"


def test_duplicate_settlement(client):
    """Settled twice by distinct events. 95 in the sample data."""
    txn = new_txn()
    client.post(
        "/events",
        json=[
            *happy_path(txn),
            event("settled", txn, minutes=200),
        ],
    )
    assert discrepancies_for(client, txn) == ["duplicate_settlement"]


def test_stuck_pending(client):
    """Initiated, then nothing, ever. 190 in the sample data."""
    txn = new_txn()
    client.post("/events", json=event("payment_initiated", txn))
    assert discrepancies_for(client, txn) == ["stuck_pending"]


def test_settled_without_processing(client):
    """Settlement with no processing — impossible if upstream is honest.

    Zero rows in the sample data, and the rule is kept deliberately: it is the
    canary for our own ingestion being wrong, and the exact corruption an
    order-dependent state machine would manufacture from out-of-order events.
    """
    txn = new_txn()
    client.post("/events", json=event("settled", txn, minutes=200))
    assert discrepancies_for(client, txn) == ["settled_without_processing"]


def test_a_transaction_can_breach_several_rules(client):
    """Failed, settled, AND settled twice. The rules are not mutually exclusive."""
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0),
            event("payment_failed", txn, minutes=10),
            event("settled", txn, minutes=200),
            event("settled", txn, minutes=205),
        ],
    )
    assert set(discrepancies_for(client, txn)) == {
        "settled_despite_failure",
        "duplicate_settlement",
    }


# ---------------------------------------------------------------------------
# The SLA is time-relative — which is why discrepancies are computed on read
# ---------------------------------------------------------------------------


def test_sla_boundary(client):
    """A transaction becomes a discrepancy purely by getting older.

    Nothing happens to it. No event arrives. It just ages past the SLA. This is
    precisely why discrepancies are computed at query time rather than flagged at
    ingest: a stored `is_broken` column would be wrong the instant the clock moved,
    with no write to trigger the correction.
    """
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0),
            event("payment_processed", txn, minutes=0),
        ],
    )
    processed_at = BASE

    def at(hours: float) -> list[str]:
        as_of = (processed_at + timedelta(hours=hours)).isoformat()
        return client.get(f"/transactions/{txn}", params={"as_of": as_of}).json()["discrepancies"]

    assert at(1) == [], "in flight after 1h — not a discrepancy"
    assert at(23) == [], "still inside the 24h SLA"
    assert at(25) == ["processed_never_settled"], "past SLA — now a discrepancy"


def test_sla_hours_is_tunable(client):
    """Ops can tighten the window without a deploy."""
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0),
            event("payment_processed", txn, minutes=0),
        ],
    )
    as_of = (BASE + timedelta(hours=6)).isoformat()

    assert client.get(f"/transactions/{txn}", params={"as_of": as_of}).json()["discrepancies"] == []
    r = client.get(
        "/reconciliation/discrepancies", params={"as_of": as_of, "sla_hours": 4}
    ).json()
    assert txn in [d["transaction_id"] for d in r["data"]]


# ---------------------------------------------------------------------------
# Out-of-order arrival
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "order",
    [
        pytest.param([0, 1, 2], id="in-order"),
        pytest.param([2, 1, 0], id="reversed"),
        pytest.param([2, 0, 1], id="settled-first"),
        pytest.param([1, 2, 0], id="initiated-last"),
    ],
)
def test_events_converge_regardless_of_arrival_order(client, db, order):
    """Three uncoordinated upstream systems means no ordering guarantee.

    The same events delivered in any order must produce the same final row. This
    is what "recompute from the log" buys: there is no state machine to confuse,
    so a late `payment_initiated` simply fills in a column the next recompute
    reads.
    """
    from sqlalchemy import text

    txn = new_txn()
    events = happy_path(txn)
    for i in order:
        client.post("/events", json=events[i])

    row = db.execute(
        text("""
            SELECT initiated_at, processed_at, failed_at, first_settled_at,
                   settled_event_count, event_count, status
            FROM transactions WHERE transaction_id = :t
        """),
        {"t": txn},
    ).one()

    assert row.status == "settled"
    assert row.event_count == 3
    assert row.settled_event_count == 1
    assert row.failed_at is None
    assert row.initiated_at is not None and row.processed_at is not None


def test_settlement_for_an_unknown_transaction_is_accepted(client):
    """A `settled` for a transaction we've never heard of must not 404.

    Across uncoordinated systems you cannot assume the initiator's events arrive
    first. Rejecting this would drop real settlements on the floor.
    """
    txn = new_txn()
    resp = client.post("/events", json=event("settled", txn, minutes=200))
    assert resp.status_code == 200
    assert resp.json()["ingested"] == 1

    detail = client.get(f"/transactions/{txn}").json()
    assert detail["settlement_status"] == "settled"
    assert detail["payment_status"] == "pending"


# ---------------------------------------------------------------------------
# Summary value reconciliation
# ---------------------------------------------------------------------------


def test_summary_value_reconciliation(client):
    """expected - settled = the gap ops must chase."""
    settled_txn, stranded_txn = new_txn(), new_txn()
    client.post("/events", json=happy_path(settled_txn))  # 1500.50, settles
    client.post(
        "/events",
        json=[
            event("payment_initiated", stranded_txn, minutes=0, amount="1000.00"),
            event("payment_processed", stranded_txn, minutes=5, amount="1000.00"),
        ],
    )

    g = client.get("/reconciliation/summary", params={"group_by": "merchant"}).json()["groups"][0]

    assert float(g["expected_settlement_amount"]) == 2500.50  # both processed
    assert float(g["settled_amount"]) == 1500.50  # only one settled
    assert float(g["unreconciled_amount"]) == 1000.00  # the stranded one


def test_unreconciled_goes_negative_when_a_failed_payment_settles(client):
    """Negative is a SIGNAL, not a bug.

    A settled-despite-failure transaction adds to settled_amount but never to
    expected — because a failed payment should never have moved money. So money
    moving that shouldn't have shows up as a negative gap. Anyone "fixing" this
    with ABS() would erase the finding.
    """
    txn = new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", txn, minutes=0, amount="800.00"),
            event("payment_failed", txn, minutes=10, amount="800.00"),
            event("settled", txn, minutes=200, amount="800.00"),
        ],
    )

    g = client.get("/reconciliation/summary", params={"group_by": "merchant"}).json()["groups"][0]

    assert float(g["expected_settlement_amount"]) == 0.0
    assert float(g["settled_amount"]) == 800.00
    assert float(g["unreconciled_amount"]) == -800.00


def test_money_is_exact_not_floating_point(client):
    """DECIMAL, not FLOAT. 0.1 + 0.2 != 0.3 in binary floating point.

    Summing amounts chosen to expose float error: a FLOAT column would drift here
    and a reconciliation service that reports the wrong total is worse than useless.
    """
    for amount in ("0.10", "0.20", "0.30"):
        txn = new_txn()
        client.post(
            "/events",
            json=[
                event("payment_initiated", txn, minutes=0, amount=amount),
                event("payment_processed", txn, minutes=5, amount=amount),
                event("settled", txn, minutes=180, amount=amount),
            ],
        )

    totals = client.get("/reconciliation/summary", params={"group_by": ""}).json()["totals"]
    assert totals["settled_amount"] == "0.60"  # exactly, as a string. Not 0.6000000000000001
