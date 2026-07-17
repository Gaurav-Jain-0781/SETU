"""POST /events — idempotent ingestion."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import Connection

from app.config import Settings, get_settings
from app.db import get_connection
from app.ingest import ingest_events
from app.schemas import EventIn, IngestResponse

router = APIRouter(tags=["events"])


@router.post(
    "/events",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest payment lifecycle events (idempotent)",
    response_description="Per-event outcome plus totals",
)
def post_events(
    conn: Annotated[Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    payload: Annotated[
        EventIn | list[EventIn],
        Body(
            description="A single event object, or an array of events.",
            openapi_examples={
                "single": {
                    "summary": "One event",
                    "value": {
                        "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
                        "event_type": "payment_initiated",
                        "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
                        "merchant_id": "merchant_2",
                        "merchant_name": "FreshBasket",
                        "amount": 15248.29,
                        "currency": "INR",
                        "timestamp": "2026-01-08T12:11:58.085567+00:00",
                    },
                },
                "batch": {
                    "summary": "A batch",
                    "value": [
                        {
                            "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
                            "event_type": "payment_initiated",
                            "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
                            "merchant_id": "merchant_2",
                            "merchant_name": "FreshBasket",
                            "amount": 15248.29,
                            "currency": "INR",
                            "timestamp": "2026-01-08T12:11:58.085567+00:00",
                        },
                        {
                            "event_id": "da46895f-4b47-4505-900e-d067f64a55eb",
                            "event_type": "payment_failed",
                            "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
                            "merchant_id": "merchant_2",
                            "merchant_name": "FreshBasket",
                            "amount": 15248.29,
                            "currency": "INR",
                            "timestamp": "2026-01-08T12:38:58.085567+00:00",
                        },
                    ],
                },
            },
        ),
    ] = ...,
) -> IngestResponse:
    """Accept one event or a batch.

    **Idempotent by `event_id`.** Re-submitting an event is safe and returns 200
    with `status: "duplicate"` for that event — not an error. Webhook senders
    retry on timeouts and 5xx; answering a safe retry with 4xx would make a
    correctly-behaving client look broken and defeat the point of idempotency.
    The caller can still see the dedup happened via the per-event `status`.

    Accepting both a single object and an array keeps one endpoint for the
    partner's real-time feed and their replay/backfill path, which otherwise
    differ only in cardinality.
    """
    events = [payload] if isinstance(payload, EventIn) else payload

    if not events:
        # An empty array is a well-formed request that asks for nothing. 200 with
        # zero counts is more useful to a batching client than an error — it lets
        # them ship an empty flush without special-casing it.
        return IngestResponse(received=0, ingested=0, duplicates=0, results=[])

    if len(events) > settings.max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Batch size {len(events)} exceeds the limit of {settings.max_batch_size}. "
                "Split the batch and retry — ingestion is idempotent, so overlapping "
                "retries are safe."
            ),
        )

    # One DB transaction for the whole batch (see app/db.get_connection). Either
    # every event lands with its projection updated, or none do. A partial batch
    # would leave the projection describing events we never durably recorded.
    return ingest_events(conn, events)
