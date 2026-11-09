# Deploying Hubline at $0

> Status: **draft**, written ahead of Step 4. The full deploy walkthrough (Render Free, Vercel Hobby, tick cron) is completed in Step 4. Everything here uses free tiers only (plan R6). The plan's §8 has the design and the arithmetic.
>
> **GitHub is on hold for now** (plan R11). Render and Vercel deploy from a GitHub repo, and the tick cron is a GitHub Actions workflow, so those parts of Step 4 wait until the account is available again.

## Neon Free (database)

1. Create a project in **AWS Asia Pacific (Singapore)**, Postgres 16. Use branch `main` for production and `dev` for checks.
2. On each branch's compute, set **autoscaling max = 0.25 CU**. That keeps usage at 0.25 CU × awake hours. The budget is about 47 of 100 CU-h/month (PLAN §8).
3. **Copy the full connection string.** In the Neon console, open **Connect** and copy the whole string. The host must start with `ep-…`. The abbreviated display (`...ap-southeast-1.aws.neon.tech`) is not a valid URL.

### Use the DIRECT (non-pooled) connection string in production

Neon offers two hosts for every branch:

| Endpoint | Host looks like | Use it for |
|---|---|---|
| **Direct** | `ep-xxxx-xxxx.c-2.ap-southeast-1.aws.neon.tech` | **Production `DATABASE_URL` on Render**, and Alembic migrations |
| Pooled (PgBouncer) | `ep-xxxx-xxxx-pooler.c-2.ap-southeast-1.aws.neon.tech` | Many short-lived clients (serverless). Not needed here. |

Why direct:
- The backend is **one long-running process** with its own SQLAlchemy pool of 5 connections, so a server-side pooler adds a hop and nothing else.
- **asyncpg relies on prepared statements.** They work through Neon's pooler today (the Step 2 Neon check passed on the `-pooler` host), but transaction-mode pooling is the classic cause of `prepared statement … does not exist` errors under load. The direct endpoint avoids the issue entirely.
- **Migrations** (`alembic upgrade head` on boot) take advisory locks and run DDL, which is best done on a direct connection.

To get the direct string, open the Connect dialog and **turn "Connection pooling" off**. Or just remove `-pooler` from the host. Keep `?sslmode=require`; `app/db/engine.py` translates it for asyncpg.

```
DATABASE_URL=postgresql://<user>:<password>@ep-xxxx-xxxx.c-2.ap-southeast-1.aws.neon.tech/neondb?sslmode=require
```

**Check a database before using it** (prints the host only, never the URL):

```bash
cd backend
uv run python -m scripts.verify_db --var NEON_DEV_DATABASE_URL
#   migrations: at revision 0004; schema diff vs models: none
#   /readyz: HTTP 200 ...
```

## Render Free (backend), Vercel Hobby (frontend), tick cron

Filled in during Step 4; see PLAN §8 and §9 (Step 4). In short:
- **Render web service:** root `backend/`, Docker runtime, **Free** instance, Singapore.
  - Production env: `ENABLE_SCHEDULER=false`, `ENABLE_AGENT_WORKER=false`, `WORK_ON_DEMAND=true`, `TICK_ON_STARTUP=true`, `TICK_SECRET=…`, `LLM_PROVIDER=groq`, `GROQ_API_KEY=…`.
  - `DATABASE_URL` is the **direct** Neon string above.
- **Vercel Hobby:** root `frontend/`, functions region `sin1`.
- **Tick cron:** `.github/workflows/tick.yml` every 30 min, 06:30–23:00 IST. It is on hold with GitHub; cron-job.org is the documented fallback (30 s timeout caveat in PLAN §8).
