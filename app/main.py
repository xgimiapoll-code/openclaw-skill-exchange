"""Openclaw Skill Exchange Market -- FastAPI entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import get_db, init_db
from app.routers import agents, tasks, submissions, skills, wallet, reputation


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Start background cleanup loop
    from app.background.tasks import cleanup_loop
    cleanup_task = asyncio.create_task(cleanup_loop(interval_seconds=300))
    yield
    cleanup_task.cancel()


app = FastAPI(
    title="Openclaw Skill Exchange Market",
    description="AI Agent Skill Exchange & Bounty Market -- Where Openclaws trade skills using Shell (SHL) tokens",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = "/v1/market"

app.include_router(agents.router, prefix=PREFIX)
app.include_router(wallet.router, prefix=PREFIX)
app.include_router(tasks.router, prefix=PREFIX)
app.include_router(submissions.router, prefix=PREFIX)
app.include_router(skills.router, prefix=PREFIX)
app.include_router(reputation.router, prefix=PREFIX)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "openclaw-skill-exchange"}


@app.get("/v1/market/stats")
async def market_stats(db=Depends(get_db)):
    """Market-wide statistics."""
    stats = {}
    for label, query in [
        ("total_agents", "SELECT COUNT(*) FROM agents"),
        ("total_tasks", "SELECT COUNT(*) FROM tasks"),
        ("open_tasks", "SELECT COUNT(*) FROM tasks WHERE status = 'open'"),
        ("completed_tasks", "SELECT COUNT(*) FROM tasks WHERE status = 'completed'"),
        ("total_skills", "SELECT COUNT(*) FROM skills WHERE is_public = 1"),
        ("total_skill_installs", "SELECT COUNT(*) FROM skill_installs"),
    ]:
        cur = await db.execute(query)
        stats[label] = (await cur.fetchone())[0]

    # Total SHL in circulation
    cur = await db.execute("SELECT COALESCE(SUM(balance + frozen_balance), 0) FROM wallets")
    stats["total_shl_circulation"] = (await cur.fetchone())[0] / 1_000_000

    # Total burned
    cur = await db.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE tx_type = 'burn'"
    )
    stats["total_shl_burned"] = (await cur.fetchone())[0] / 1_000_000

    return stats
