"""Event builders.

Tests should read as scenarios ("a payment that failed then settled anyway"), not
as walls of JSON. Everything here exists to keep the test bodies about behaviour.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

BASE = datetime(2026, 1, 8, 12, 0, 0, tzinfo=UTC)


def event(
    event_type: str,
    transaction_id: str,
    *,
    event_id: str | None = None,
    minutes: int = 0,
    amount: str = "1500.50",
    merchant_id: str = "merchant_1",
    merchant_name: str = "QuickMart",
) -> dict:
    """One event on the wire, exactly as a partner would send it."""
    return {
        "event_id": event_id or str(uuid4()),
        "event_type": event_type,
        "transaction_id": transaction_id,
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "amount": amount,
        "currency": "INR",
        "timestamp": (BASE + timedelta(minutes=minutes)).isoformat(),
    }


def new_txn() -> str:
    return str(uuid4())


def happy_path(txn: str) -> list[dict]:
    """initiated -> processed -> settled. No discrepancy."""
    return [
        event("payment_initiated", txn, minutes=0),
        event("payment_processed", txn, minutes=5),
        event("settled", txn, minutes=180),
    ]
