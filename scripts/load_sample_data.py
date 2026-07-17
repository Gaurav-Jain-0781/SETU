#!/usr/bin/env python3
"""Load sample_events.json into the database.

Goes through the same `ingest_events()` path the HTTP API uses, rather than a
separate bulk-import shortcut. That is the point: if the loader had its own
faster-but-different insert logic, then a successful load would prove nothing
about whether the API is correct. Same code path means loading the file is itself
a test of ingestion.

Chunked rather than one giant statement so that memory and transaction size stay
bounded — the same reason POST /events caps batch size.

Usage:
    python -m scripts.load_sample_data                      # load sample_events.json
    python -m scripts.load_sample_data --file other.json
    python -m scripts.load_sample_data --truncate           # wipe first
    python -m scripts.load_sample_data --chunk-size 500
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import apply_schema, get_engine  # noqa: E402
from app.ingest import ingest_events  # noqa: E402
from app.schemas import EventIn  # noqa: E402

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "sample_events.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    ap.add_argument("--chunk-size", type=int, default=1000)
    ap.add_argument(
        "--truncate",
        action="store_true",
        help="Delete all data first. Without this the load is idempotent: "
        "re-running reports every event as a duplicate and changes nothing.",
    )
    args = ap.parse_args()

    if not args.file.exists():
        print(f"error: {args.file} not found", file=sys.stderr)
        return 1

    print(f"Applying schema to {_safe_dsn()}")
    apply_schema()

    if args.truncate:
        print("Truncating existing data")
        with get_engine().begin() as conn:
            # RESTART IDENTITY CASCADE: transactions and payment_events both FK to
            # merchants, so they must go together or the FK blocks the wipe.
            conn.execute(
                text("TRUNCATE payment_events, transactions, merchants RESTART IDENTITY CASCADE")
            )

    raw = json.loads(args.file.read_text())
    print(f"Read {len(raw):,} events from {args.file.name}")

    # Validate everything up front, before writing anything. A file that is 90%
    # good and 10% malformed should fail loudly at the start rather than leave a
    # half-loaded database behind.
    events: list[EventIn] = []
    errors = 0
    for i, rec in enumerate(raw):
        try:
            events.append(EventIn.model_validate(rec))
        except ValidationError as e:
            errors += 1
            if errors <= 5:
                print(f"  invalid event at index {i}: {e.errors()[0]['msg']}", file=sys.stderr)
    if errors:
        print(f"error: {errors} invalid event(s); nothing was loaded", file=sys.stderr)
        return 1

    totals = {"received": 0, "ingested": 0, "duplicates": 0}
    start = time.perf_counter()
    engine = get_engine()

    for i in range(0, len(events), args.chunk_size):
        chunk = events[i : i + args.chunk_size]
        # One transaction per chunk. A failure mid-load rolls back only the
        # current chunk; because ingestion is idempotent, simply re-running the
        # loader safely resumes rather than double-counting the chunks that
        # already landed.
        with engine.connect() as conn, conn.begin():
            res = ingest_events(conn, chunk)
        totals["received"] += res.received
        totals["ingested"] += res.ingested
        totals["duplicates"] += res.duplicates
        done = min(i + args.chunk_size, len(events))
        print(f"  {done:,}/{len(events):,}", end="\r", flush=True)

    elapsed = time.perf_counter() - start
    print(" " * 30, end="\r")
    print(
        f"\nLoaded in {elapsed:.2f}s ({len(events) / elapsed:,.0f} events/s)\n"
        f"  received:   {totals['received']:,}\n"
        f"  ingested:   {totals['ingested']:,}\n"
        f"  duplicates: {totals['duplicates']:,}  (idempotency absorbed these)"
    )

    with engine.connect() as conn:
        row = conn.execute(
            text("""
                SELECT (SELECT count(*) FROM merchants)      AS merchants,
                       (SELECT count(*) FROM transactions)   AS transactions,
                       (SELECT count(*) FROM payment_events) AS events
            """)
        ).one()
        print(
            f"\nDatabase now holds:\n"
            f"  merchants:      {row.merchants:,}\n"
            f"  transactions:   {row.transactions:,}\n"
            f"  payment_events: {row.events:,}"
        )
        # ANALYZE so the planner has real statistics. Without it Postgres uses
        # defaults from an empty table and may pick a sequential scan over the
        # partial indexes — the queries would be correct but the timings would
        # misrepresent the design.
        print("\nRunning ANALYZE for planner statistics")
    with engine.begin() as conn:
        conn.execute(text("ANALYZE merchants, transactions, payment_events"))

    print("Done.")
    return 0


def _safe_dsn() -> str:
    """DSN with the password redacted — this gets printed to CI logs."""
    from app.config import get_settings

    url = get_settings().database_url
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        creds, host = rest.rsplit("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}://{user}:***@{host}"
    return url


if __name__ == "__main__":
    raise SystemExit(main())
