"""Environment-driven configuration.

Everything that differs between laptop, CI and Render lives here and nowhere
else, so deploying is a matter of setting env vars rather than editing code.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Port 3307 matches docker-compose (3307 on the host so it can't collide with
    # a MySQL already running locally).
    database_url: str = "mysql+pymysql://setu:setu@localhost:3307/setu"

    # Settlement SLA. A payment that has been processed but not settled is only a
    # discrepancy once it is past this age — before that it is simply in flight.
    #
    # 24h is chosen on evidence, not vibes: in the sample data every settlement
    # lands within 6.00h of processing (p99 = 5.95h), while every never-settled
    # transaction is >=17.3h old. 24h sits in that gap with a 4x margin over
    # observed p99 and matches the T+1 convention in Indian payments. Overridable
    # per-request via ?sla_hours= so ops can tighten or loosen without a deploy.
    settlement_sla_hours: int = 24

    # Hard ceiling on page size. Prevents a caller from asking for the entire
    # table in one response and turning a paginated endpoint into a full scan.
    max_page_size: int = 100

    # Cap on events accepted in a single POST /events batch. Bounds request body
    # size, memory, and how long a single DB transaction can hold locks.
    max_batch_size: int = 1000

    db_pool_size: int = 5
    db_max_overflow: int = 10

    # Applies sql/schema.sql at startup. True is right for this service: the DDL
    # is idempotent (CREATE TABLE IF NOT EXISTS) and it means a fresh Render
    # deploy or a fresh container is self-provisioning with no manual step. A
    # system with real migration history would use Alembic instead — see README
    # § Tradeoffs.
    auto_migrate: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
