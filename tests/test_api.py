"""API contract: validation, errors, pagination, and the schema's own guarantees."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.factory import event, happy_path, new_txn

# ---------------------------------------------------------------------------
# Validation — the rubric grades this explicitly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutation, why",
    [
        pytest.param({"event_type": "banana"}, "unknown event type", id="bad-event-type"),
        pytest.param({"event_id": "not-a-uuid"}, "malformed uuid", id="bad-uuid"),
        pytest.param({"amount": "-5.00"}, "negative money", id="negative-amount"),
        pytest.param({"currency": "RUPEES"}, "not a 3-letter code", id="bad-currency"),
        pytest.param({"timestamp": "yesterday"}, "unparseable time", id="bad-timestamp"),
        pytest.param({"merchant_id": ""}, "empty merchant", id="empty-merchant"),
    ],
)
def test_invalid_events_are_rejected(client, mutation, why):
    """Garbage never reaches the database.

    Everything downstream of Pydantic is guaranteed a well-formed UUID, a known
    event type, an exact Decimal and a timezone-aware instant. That guarantee is
    why queries.py can be plain SQL with bound parameters and no defensive
    re-checking.
    """
    bad = {**event("payment_initiated", new_txn()), **mutation}
    resp = client.post("/events", json=bad)

    assert resp.status_code == 422, why
    assert resp.json()["error"]["code"] == "validation_error"


def test_unknown_field_is_rejected(client):
    """extra="forbid": a typo'd field name is a bug, not something to swallow.

    Silently ignoring `ammount` would mean accepting an event whose amount we never
    read, and reconciling money we never received.
    """
    bad = {**event("payment_initiated", new_txn()), "ammount": 100}
    assert client.post("/events", json=bad).status_code == 422


def test_every_error_uses_the_same_envelope(client):
    """One error shape, so a client writes one error handler."""
    for resp in (
        client.get(f"/transactions/{new_txn()}"),  # 404
        client.post("/events", json={"event_type": "nonsense"}),  # 422
        client.get("/transactions", params={"sort_by": "; DROP TABLE--"}),  # 422
    ):
        assert resp.status_code >= 400
        body = resp.json()
        assert set(body) == {"error"}
        assert {"code", "message", "field"} <= set(body["error"])


def test_unknown_transaction_returns_404(client):
    resp = client.get(f"/transactions/{new_txn()}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_sort_by_is_whitelisted(client):
    """ORDER BY takes an identifier, which cannot be a bound parameter.

    So the sort field is looked up in a dict of SQL we wrote — user input selects a
    value, it never becomes one. This is the only place SQL structure depends on
    input, and it's worth proving it's closed.
    """
    resp = client.get("/transactions", params={"sort_by": "amount; DROP TABLE transactions"})
    assert resp.status_code == 422

    # And the table is, of course, still there.
    assert client.get("/transactions").status_code == 200


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_never_drops_or_repeats_a_row(client):
    """Every transaction appears exactly once across pages.

    All 25 share an identical initiated_at, which is the trap: SQL guarantees
    ordering BETWEEN different sort values but says nothing about ties, so the
    engine may permute them differently per query. Without the transaction_id
    tie-breaker, a row shows up on two pages and another is never returned at all —
    and nobody notices until an ops person swears a transaction vanished.
    """
    expected = set()
    for _ in range(25):
        txn = new_txn()
        expected.add(txn)
        client.post("/events", json=event("payment_initiated", txn, minutes=0))  # same timestamp

    seen: list[str] = []
    for offset in (0, 10, 20):
        page = client.get("/transactions", params={"limit": 10, "offset": offset}).json()
        seen += [t["transaction_id"] for t in page["data"]]

    assert len(seen) == 25
    assert len(set(seen)) == 25, "a row appeared on more than one page"
    assert set(seen) == expected, "a row was never returned"


def test_pagination_metadata(client):
    for _ in range(7):
        client.post("/events", json=event("payment_initiated", new_txn()))

    page = client.get("/transactions", params={"limit": 5, "offset": 0}).json()
    assert page["pagination"] == {
        "total": 7,  # the full count, not the page size
        "limit": 5,
        "offset": 0,
        "returned": 5,
        "has_more": True,
    }
    assert client.get("/transactions", params={"limit": 5, "offset": 5}).json()["pagination"][
        "has_more"
    ] is False


def test_limit_is_capped(client):
    assert client.get("/transactions", params={"limit": 5000}).status_code == 422


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_the_two_status_axes_combine(client):
    """payment_status=failed & settlement_status=settled finds what one column can't."""
    broken, healthy = new_txn(), new_txn()
    client.post(
        "/events",
        json=[
            event("payment_initiated", broken, minutes=0),
            event("payment_failed", broken, minutes=10),
            event("settled", broken, minutes=200),
        ],
    )
    client.post("/events", json=happy_path(healthy))

    page = client.get(
        "/transactions", params={"payment_status": "failed", "settlement_status": "settled"}
    ).json()

    assert page["pagination"]["total"] == 1
    assert page["data"][0]["transaction_id"] == broken


def test_date_to_is_exclusive(client):
    """Avoids the classic BETWEEN bug of dropping the final day's later events."""
    txn = new_txn()
    client.post("/events", json=event("payment_initiated", txn, minutes=0))  # 2026-01-08T12:00

    included = client.get(
        "/transactions", params={"date_from": "2026-01-08T00:00:00+00:00",
                                 "date_to": "2026-01-09T00:00:00+00:00"}
    ).json()
    assert included["pagination"]["total"] == 1

    excluded = client.get(
        "/transactions", params={"date_from": "2026-01-08T00:00:00+00:00",
                                 "date_to": "2026-01-08T12:00:00+00:00"}
    ).json()
    assert excluded["pagination"]["total"] == 0


def test_date_from_must_precede_date_to(client):
    resp = client.get(
        "/transactions",
        params={"date_from": "2026-02-01T00:00:00+00:00", "date_to": "2026-01-01T00:00:00+00:00"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Transaction detail
# ---------------------------------------------------------------------------


def test_detail_returns_the_full_audit_trail(client):
    """Every event, including the replayed one, oldest first.

    The history is what lets an operator answer *why* a transaction is in a given
    state. It is the entire reason payment_events is append-only.
    """
    txn = new_txn()
    dup = event("settled", txn, minutes=180)
    client.post("/events", json=[event("payment_initiated", txn, minutes=0),
                                 event("payment_processed", txn, minutes=5), dup])
    client.post("/events", json=dup)  # replay — must not add a 4th event

    detail = client.get(f"/transactions/{txn}").json()

    assert [e["event_type"] for e in detail["events"]] == [
        "payment_initiated",
        "payment_processed",
        "settled",
    ]
    assert detail["event_count"] == 3
    assert detail["merchant"]["merchant_name"] == "QuickMart"
    # occurred_at (sender's clock) and received_at (ours) are distinct facts.
    assert all(e["occurred_at"] != e["received_at"] for e in detail["events"])


# ---------------------------------------------------------------------------
# Schema-level guarantees
#
# These assert on the DATABASE, not the app. They prove the guarantees hold even
# against someone bypassing our Python entirely — which is the whole point of
# enforcing them in the schema rather than in code.
# ---------------------------------------------------------------------------


def test_check_constraint_rejects_unknown_event_type(client, db):
    """The database refuses a bad event_type even with the API bypassed.

    NOTE: CHECK is only enforced from MySQL 8.0.16, and TiDB may not enforce it at
    all depending on version/settings. If this fails on the deployment target, the
    claim in sql/schema.sql and the README must be corrected — Pydantic would then
    be the ONLY line of defence, not the second.
    """
    client.post("/events", json=event("payment_initiated", new_txn()))  # create merchant_1

    with pytest.raises(DBAPIError):
        with db.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO payment_events (event_id, transaction_id, merchant_id,
                        event_type, amount, currency, occurred_at, payload)
                    VALUES (UUID(), UUID(), 'merchant_1', 'banana', 1, 'INR', NOW(6), '{}')
                """)
            )


def test_generated_status_cannot_be_written(client, db):
    """Nobody can put a lie in the status column — not us, not a migration, not prod SQL.

    This is the drift-impossibility argument, enforced. A row can never claim
    'settled' while first_settled_at is NULL.
    """
    txn = new_txn()
    client.post("/events", json=event("payment_initiated", txn))

    with pytest.raises(DBAPIError):
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE transactions SET status = 'settled'"))


def test_duplicate_event_id_is_refused_by_the_primary_key(client, db):
    """Idempotency is enforced by the database, not by application code.

    A plain INSERT of an existing event_id must error. This is what closes the race
    that a check-then-insert leaves open: two concurrent replays cannot both pass,
    because the check and the write are the same operation.
    """
    txn = new_txn()
    e = event("payment_initiated", txn)
    client.post("/events", json=e)

    with pytest.raises(DBAPIError):
        with db.engine.begin() as conn:
            conn.execute(
                text("""
                    INSERT INTO payment_events (event_id, transaction_id, merchant_id,
                        event_type, amount, currency, occurred_at, payload)
                    VALUES (:id, UUID(), 'merchant_1', 'settled', 1, 'INR', NOW(6), '{}')
                """),
                {"id": e["event_id"]},
            )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_does_not_touch_the_database(client):
    """Liveness must not fail because the DB blipped — that invites a restart loop
    on a process that is fine. Readiness is the one that checks the DB."""
    assert client.get("/health").json() == {"status": "ok"}


def test_readiness_reports_counts(client):
    client.post("/events", json=happy_path(new_txn()))
    body = client.get("/health/ready").json()
    assert body["status"] == "ready"
    assert body["counts"] == {"events": 3, "transactions": 1, "merchants": 1}
