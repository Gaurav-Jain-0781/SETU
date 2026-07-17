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

    # TLS for the database connection.
    #
    # Off for local Docker (the DB is on a private network on the same host, and
    # requiring certs there would be ceremony). ON for any managed provider —
    # TiDB Cloud refuses non-TLS connections outright, and it should: without it
    # the credentials and every row cross the public internet in the clear.
    #
    # We verify against certifi's CA bundle rather than shipping a provider CA
    # file. Fewer moving parts, one less thing to expire, and it works for any
    # managed MySQL rather than just the one we happened to pick.
    db_ssl: bool = False

    @property
    def connect_args(self) -> dict:
        if not self.db_ssl:
            return {}
        import certifi

        # ssl.ca is what makes this real: it verifies the server's certificate
        # against a trusted root. Encrypting without verifying would stop passive
        # eavesdropping but not an active man-in-the-middle.
        return {"ssl": {"ca": certifi.where()}}

    # Applies sql/schema.sql at startup. True is right for this service: the DDL
    # is idempotent (CREATE TABLE IF NOT EXISTS) and it means a fresh Render
    # deploy or a fresh container is self-provisioning with no manual step. A
    # system with real migration history would use Alembic instead — see README
    # § Tradeoffs.
    auto_migrate: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
