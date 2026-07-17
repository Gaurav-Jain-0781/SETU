# Deployment

```
   Laptop ──(load 10k events, once)──►  TiDB Cloud Starter (free, ap-southeast-1)
                                                  ▲
                                                  │ DATABASE_URL (TLS)
   Reviewer ──────────────────────────►  Render (free web service, singapore)
                                          runs ./Dockerfile
```

Config lives in [`render.yaml`](../render.yaml) rather than a dashboard, so the
deployment is reviewable and reproducible instead of existing only in one person's
browser history.

## Why the database isn't on Render

**Render's managed database is PostgreSQL-only.** This service runs MySQL, so the
database lives on **TiDB Cloud Starter** — free, no credit card, and it speaks the MySQL
wire protocol, so application code, PyMySQL and MySQL Workbench connect unchanged.

The other free MySQL host (Aiven) was rejected on a specific risk: its free tier *powers
off after inactivity* and appears to need a manual restart. For a demo a reviewer might
open days later, that's a submission that silently dies. TiDB is serverless — it scales
to zero and wakes on connection, so it can't become unreachable while nobody's looking.

**TiDB is MySQL-*compatible*, not MySQL.** That cost us something real.

## ⚠️ Required: `tidb_enable_check_constraint`

**TiDB does not enforce `CHECK` constraints by default.** It parses them and silently
ignores them. An `event_type` of `'banana'` that MySQL 8.4 refuses goes straight in.

Not theoretical — caught by `test_check_constraint_rejects_unknown_event_type`, which
passed on MySQL 8.4 and **failed on TiDB**, before anything was deployed.

```sql
SET GLOBAL tidb_enable_check_constraint = ON;
```

Global and persistent, but it's **cluster state that lives nowhere in this repo**. A
freshly provisioned cluster silently loses `CHECK` enforcement. A genuine footgun of the
MySQL-compatible-but-not-MySQL choice.

Two things keep it honest:
1. It's a documented step (here, and below).
2. The test suite is the canary — run it against any new cluster and that test fails
   loudly if the setting is missing.

If it were ever missing, Pydantic still rejects unknown event types before they reach the
database. `CHECK` is defence in depth, not the only line — but "second line of defence"
is only true if it's actually there.

## First-time setup

**1. TiDB Cloud** — <https://tidbcloud.com>, create a **Starter** cluster in
`ap-southeast-1`, then:

```sql
CREATE DATABASE setu;
SET GLOBAL tidb_enable_check_constraint = ON;   -- not optional
```

**2. Load the sample data** (once, from a laptop — not on every boot):

```bash
export DATABASE_URL="mysql+pymysql://<user>:<pass>@gateway01.<region>.prod.aws.tidbcloud.com:4000/setu"
export DB_SSL=true
python -m scripts.load_sample_data
```

```
received:   10,355
ingested:   10,165
duplicates:    190   ← idempotency absorbing the duplicates in the source file
merchants:       5
transactions: 3,800
```

~15s over the public internet (vs 0.9s locally — that's round-trip latency to Singapore,
not slow code). Idempotent, so re-running is harmless: every event reports as a duplicate
and nothing changes.

`--truncate` wipes and reloads, which resets the demo to exactly 10,165 / 3,800 if demo
traffic has drifted it.

**3. Render** — New → Blueprint → point at this repo. `render.yaml` configures everything
except the one secret:

| env var | value |
|---|---|
| `DATABASE_URL` | the TiDB URL — **set in the dashboard, never committed** |
| `DB_SSL` | `true` (from `render.yaml`) |
| `AUTO_MIGRATE` | `true` (from `render.yaml`) |

## Verifying a deploy

```bash
curl https://setu-recon.onrender.com/health/ready
# {"status":"ready","counts":{"events":10165,"transactions":3800,"merchants":5}}
```

`events: 10165` from a 10,355-event file is idempotency, visible from the outside.

## Connection handling

**TLS is mandatory** — TiDB refuses plaintext, correctly: otherwise credentials and every
row would cross the public internet in the clear. Verified against `certifi`'s CA bundle
rather than shipping a provider CA file: fewer moving parts, nothing to expire, works
against any managed MySQL. Confirmed live — `Ssl_cipher = TLS_AES_128_GCM_SHA256`.

**`pool_pre_ping=True`** — managed databases drop idle connections. Without it the pool
hands out a socket the server already closed, and the first request after a quiet period
fails with *"MySQL server has gone away"*. On a free tier that sleeps, that's not an edge
case; it's every morning.

**The sample data is loaded out of band, not on boot.** Re-running the loader on every
deploy would add ~10k round trips to a cold start Render already times, to re-insert data
that's already there. Idempotent, so it'd be *safe* — just pointless and slow.

## Known limits

- **Render free sleeps after ~15 min idle.** First request wakes it — **~30–50s**, then
  normal. If the demo seems dead, it's waking up; retry once.
- **Every query crosses Render → TiDB over the internet.** Both in Singapore to keep the
  hop short, but it's a network round trip where local Docker had none. This is why
  `docker compose up` feels faster than the live URL.
- **TiDB Starter has a monthly Request Unit quota.** Far beyond a demo's needs.
