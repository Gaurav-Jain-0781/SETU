"""Request validation and response contracts.

Pydantic is doing real work here, not decoration: everything downstream of these
models is guaranteed a well-formed UUID, a known event_type, an exact Decimal
amount and a timezone-aware instant. That guarantee is why queries.py can be
plain SQL with bound parameters and no defensive re-checking.
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    PAYMENT_INITIATED = "payment_initiated"
    PAYMENT_PROCESSED = "payment_processed"
    PAYMENT_FAILED = "payment_failed"
    SETTLED = "settled"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class SettlementStatus(StrEnum):
    UNSETTLED = "unsettled"
    SETTLED = "settled"


class TransactionStatus(StrEnum):
    """Flattened status for convenience filtering on GET /transactions."""

    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"
    SETTLED = "settled"


class DiscrepancyType(StrEnum):
    PROCESSED_NEVER_SETTLED = "processed_never_settled"
    SETTLED_DESPITE_FAILURE = "settled_despite_failure"
    DUPLICATE_SETTLEMENT = "duplicate_settlement"
    STUCK_PENDING = "stuck_pending"
    SETTLED_WITHOUT_PROCESSING = "settled_without_processing"


class IngestOutcome(StrEnum):
    INGESTED = "ingested"
    DUPLICATE = "duplicate"


# Money: exact decimal, never float. 14 digits with 2 decimal places covers
# ~1e12 (₹999 billion) which is far beyond any single retail payment.
Amount = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]

# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


class EventIn(BaseModel):
    """A single payment lifecycle event.

    Field names mirror the partner's wire format exactly (`timestamp`, not
    `occurred_at`) so integrators need no translation layer. The rename to
    occurred_at happens at the DB boundary, where the distinction from
    received_at starts to matter.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: EventType
    transaction_id: UUID
    merchant_id: str = Field(min_length=1, max_length=64)
    merchant_name: str = Field(min_length=1, max_length=255)
    amount: Amount
    currency: str = Field(min_length=3, max_length=3)
    timestamp: datetime

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        v = v.upper()
        if not v.isalpha():
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return v

    @field_validator("timestamp")
    @classmethod
    def _require_instant(cls, v: datetime) -> datetime:
        # A naive timestamp in a payments feed is ambiguous by definition. We
        # assume UTC rather than reject, because rejecting mid-integration is a
        # worse failure mode than a documented assumption — but we normalise
        # immediately so nothing downstream ever sees a naive datetime.
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)


class EventResult(BaseModel):
    event_id: UUID
    status: IngestOutcome
    transaction_id: UUID


class IngestResponse(BaseModel):
    """Per-event outcomes plus totals.

    Duplicates are reported as a successful outcome, not an error — see
    README § Idempotency for why a replay must not look like a failure.
    """

    received: int
    ingested: int
    duplicates: int
    results: list[EventResult]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class MerchantOut(BaseModel):
    merchant_id: str
    merchant_name: str


class TransactionOut(BaseModel):
    transaction_id: UUID
    merchant: MerchantOut
    amount: Decimal
    currency: str

    status: TransactionStatus
    payment_status: PaymentStatus
    settlement_status: SettlementStatus

    initiated_at: datetime | None
    processed_at: datetime | None
    failed_at: datetime | None
    first_settled_at: datetime | None
    last_settled_at: datetime | None

    event_count: int
    settled_event_count: int
    last_event_at: datetime


class EventOut(BaseModel):
    event_id: UUID
    event_type: EventType
    amount: Decimal
    currency: str
    occurred_at: datetime
    received_at: datetime


class TransactionDetailOut(TransactionOut):
    """Transaction plus its full event history, oldest first.

    Includes discrepancies so a single fetch answers "what is wrong with this
    one?" without a second call to the reconciliation endpoint.
    """

    discrepancies: list[DiscrepancyType]
    events: list[EventOut]


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int
    has_more: bool


class TransactionListOut(BaseModel):
    pagination: PageMeta
    data: list[TransactionOut]


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

SummaryDimension = Literal["merchant", "date", "status", "payment_status", "settlement_status"]


class SummaryGroup(BaseModel):
    """One row of the reconciliation summary.

    Dimension fields are populated only when grouped by; the money fields are
    always present.

    The value reconciliation is the point of this endpoint:
        expected_settlement_amount  money that SHOULD have settled (processed)
        settled_amount              money that DID settle
        unreconciled_amount         expected - settled, i.e. the gap ops must chase

    unreconciled_amount can legitimately go negative: a settled-despite-failure
    transaction contributes to settled_amount but never to expected, which is
    exactly the signal you want — money moved that shouldn't have.
    """

    merchant_id: str | None = None
    merchant_name: str | None = None
    date: str | None = None
    status: str | None = None
    payment_status: str | None = None
    settlement_status: str | None = None

    transaction_count: int
    total_amount: Decimal

    processed_count: int
    failed_count: int
    pending_count: int
    settled_count: int
    unsettled_count: int

    expected_settlement_amount: Decimal
    settled_amount: Decimal
    unreconciled_amount: Decimal


class SummaryTotals(BaseModel):
    transaction_count: int
    total_amount: Decimal
    expected_settlement_amount: Decimal
    settled_amount: Decimal
    unreconciled_amount: Decimal


class SummaryOut(BaseModel):
    group_by: list[SummaryDimension]
    filters: dict[str, str | None]
    totals: SummaryTotals
    groups: list[SummaryGroup]


class DiscrepancyOut(BaseModel):
    """A transaction whose payment and settlement state disagree.

    `discrepancies` is a list because a transaction can breach more than one rule
    at once (e.g. settled twice AND settled despite failing).
    """

    transaction_id: UUID
    merchant: MerchantOut
    amount: Decimal
    currency: str
    payment_status: PaymentStatus
    settlement_status: SettlementStatus
    discrepancies: list[DiscrepancyType]
    detail: str

    initiated_at: datetime | None
    processed_at: datetime | None
    failed_at: datetime | None
    first_settled_at: datetime | None
    settled_event_count: int
    age_hours: float | None


class DiscrepancyCount(BaseModel):
    type: DiscrepancyType
    count: int
    total_amount: Decimal


class DiscrepancyListOut(BaseModel):
    as_of: datetime
    sla_hours: int
    pagination: PageMeta
    counts_by_type: list[DiscrepancyCount]
    data: list[DiscrepancyOut]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
