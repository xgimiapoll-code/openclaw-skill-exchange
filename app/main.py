"""Openclaw Skill Exchange Market -- FastAPI entry point."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import os

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.db import get_db, init_db
from app.logging_config import setup_logging
from app.routers import agents, tasks, submissions, skills, wallet, reputation, disputes, ws, bridge, collaboration, guide, mcp

logger = logging.getLogger(__name__)
_start_time = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("OpenClaw Skill Exchange starting up")
    await init_db()
    # Start background cleanup loop
    from app.background.tasks import cleanup_loop
    cleanup_task = asyncio.create_task(cleanup_loop(interval_seconds=300))
    yield
    cleanup_task.cancel()


app = FastAPI(
    title="Openclaw Skill Exchange Market",
    description="AI Agent Collaboration & Bounty Market — Task decomposition, fair-share distribution, skill marketplace, and SHL tokens on Base L2",
    version="0.5.0",
    lifespan=lifespan,
)

from app.config import config as _cfg

_cors_origins = [o.strip() for o in _cfg.cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    if request.url.path not in ("/healthz", "/"):
        logger.info(
            "%s %s %d %.0fms",
            request.method, request.url.path, response.status_code, duration_ms,
            extra={"method": request.method, "path": request.url.path,
                   "status_code": response.status_code, "duration_ms": round(duration_ms)},
        )
    return response


PREFIX = "/v1/market"

app.include_router(agents.router, prefix=PREFIX)
app.include_router(wallet.router, prefix=PREFIX)
app.include_router(tasks.router, prefix=PREFIX)
app.include_router(submissions.router, prefix=PREFIX)
app.include_router(skills.router, prefix=PREFIX)
app.include_router(reputation.router, prefix=PREFIX)
app.include_router(disputes.router, prefix=PREFIX)
app.include_router(disputes.task_disputes, prefix=PREFIX)
app.include_router(bridge.router, prefix=PREFIX)
app.include_router(collaboration.router, prefix=PREFIX)
app.include_router(ws.router, prefix=PREFIX)
app.include_router(guide.router, prefix=PREFIX)
# MCP / discovery endpoints at root (no prefix)
app.include_router(mcp.router)

# Static assets
_static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


@app.get("/")
async def landing_page():
    """Landing page — serves static HTML with live API-fetched data."""
    return FileResponse(os.path.join(_static_dir, "index.html"), media_type="text/html")


@app.get("/healthz")
async def healthz(db=Depends(get_db)):
    """Health check with DB connectivity verification."""
    checks = {"db": "fail"}
    try:
        cur = await db.execute("SELECT 1")
        await cur.fetchone()
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"error: {e}"

    from app.db import _IS_PG
    status = "ok" if checks["db"] == "ok" else "degraded"
    uptime_seconds = round(time.monotonic() - _start_time)
    return {
        "status": status,
        "service": "openclaw-skill-exchange",
        "version": app.version,
        "db_backend": "postgresql" if _IS_PG else "sqlite",
        "checks": checks,
        "uptime_seconds": uptime_seconds,
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics(db=Depends(get_db)):
    """Prometheus-compatible metrics endpoint."""
    lines = []

    cur = await db.execute("SELECT COUNT(*) FROM agents")
    agents_count = (await cur.fetchone())[0]
    lines.append(f"openclaw_agents_total {agents_count}")

    cur = await db.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
    )
    for row in await cur.fetchall():
        lines.append(f'openclaw_tasks_total{{status="{row["status"]}"}} {row["cnt"]}')

    cur = await db.execute("SELECT COUNT(*) FROM skills WHERE is_public = 1")
    skills_count = (await cur.fetchone())[0]
    lines.append(f"openclaw_skills_total {skills_count}")

    cur = await db.execute("SELECT COALESCE(SUM(balance + frozen_balance), 0) FROM wallets")
    shl_micro = (await cur.fetchone())[0]
    lines.append(f"openclaw_shl_circulation_micro {shl_micro}")

    cur = await db.execute("SELECT COUNT(*) FROM disputes WHERE status IN ('open', 'under_review')")
    disputes_count = (await cur.fetchone())[0]
    lines.append(f"openclaw_disputes_open {disputes_count}")

    uptime = round(time.monotonic() - _start_time)
    lines.append(f"openclaw_uptime_seconds {uptime}")

    return "\n".join(lines) + "\n"


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
        ("total_disputes", "SELECT COUNT(*) FROM disputes"),
        ("open_disputes", "SELECT COUNT(*) FROM disputes WHERE status IN ('open', 'under_review')"),
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

    # Bridge stats
    from app.blockchain.provider import is_blockchain_enabled
    stats["blockchain_enabled"] = is_blockchain_enabled()

    cur = await db.execute("SELECT COUNT(*) FROM bridge_requests WHERE status = 'completed'")
    stats["total_bridge_transfers"] = (await cur.fetchone())[0]

    cur = await db.execute("SELECT COUNT(*) FROM settlement_batches WHERE status = 'confirmed'")
    stats["total_settlements"] = (await cur.fetchone())[0]

    return stats
