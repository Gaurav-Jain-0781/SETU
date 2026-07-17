"""FastAPI application: wiring, lifespan, and uniform error responses."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import apply_schema
from app.routers import events, health, reconciliation, transactions

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
)
log = logging.getLogger("setu")

DESCRIPTION = """
Payment event ingestion and reconciliation for a Setu partner integration.

**Ingestion is idempotent by `event_id`.** Replaying an event is a no-op that
returns `200` with `status: "duplicate"` — never an error, so a retrying webhook
sender is never told it did something wrong.

**Events are the source of truth.** `payment_events` is append-only; the
`transactions` table is a projection merged from it with commutative operators
(`LEAST`/`GREATEST`/`+`), which makes ingestion independent of both event order
and delivery count.

**Payment and settlement are independent axes.** That is what makes a
contradiction like "failed but settled" representable — and therefore findable at
`/reconciliation/discrepancies`.

See `/reconciliation/rules` for the exact SQL behind each discrepancy class.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.auto_migrate:
        # Idempotent DDL on boot. Makes a fresh container or a fresh Render
        # deploy self-provisioning: no manual migrate step to forget, which is
        # the difference between a reviewer seeing the service work and seeing a
        # 500. See README § Tradeoffs for why this isn't Alembic.
        log.info("Applying schema (auto_migrate=True)")
        apply_schema()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Setu Reconciliation Service",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.include_router(health.router)
    app.include_router(events.router)
    app.include_router(transactions.router)
    app.include_router(reconciliation.router)

    # -- Uniform error envelope ---------------------------------------------
    # Every failure returns {"error": {"code", "message", "field"}}. A client
    # integrating against this should be able to write one error handler, not one
    # per endpoint — and never have to distinguish a validation failure from a
    # crash by shape-sniffing the response body.

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"][1:]) or None
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "validation_error",
                    "message": first["msg"],
                    "field": field,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Without this, HTTPExceptions raised in routers would return FastAPI's
        # default {"detail": ...} and break the envelope every other error keeps.
        codes = {
            404: "not_found",
            409: "conflict",
            413: "payload_too_large",
            422: "validation_error",
        }
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": codes.get(exc.status_code, "error"),
                    "message": exc.detail if isinstance(exc.detail, str) else str(exc.detail),
                    "field": None,
                }
            },
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error(_: Request, exc: IntegrityError) -> JSONResponse:
        # Reaching here means a constraint we rely on was violated in a way the
        # ON CONFLICT paths don't cover — e.g. an event referencing a merchant
        # that was never upserted. Surface it as a 409 rather than a 500: it is
        # the caller's data that conflicts, not our server that broke.
        log.warning("Integrity error: %s", exc.orig)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "code": "integrity_error",
                    "message": "Request conflicts with existing data.",
                    "field": None,
                }
            },
        )

    @app.exception_handler(DBAPIError)
    async def _db_error(_: Request, exc: DBAPIError) -> JSONResponse:
        # Log the driver's message, return a generic one. Database errors can
        # quote row values and schema internals; echoing them to a client leaks
        # data and hands an attacker a free schema map.
        log.exception("Database error", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "database_unavailable",
                    "message": "The database is currently unavailable. Retry shortly.",
                    "field": None,
                }
            },
        )

    return app


app = create_app()
