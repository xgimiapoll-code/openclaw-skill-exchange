"""Reputation system endpoints."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_agent
from app.background.tasks import recalculate_reputation
from app.db import get_db
from app.services.rate_limiter import get_tier

router = APIRouter(prefix="/reputation", tags=["reputation"])


@router.get("/me")
async def get_my_reputation(
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get current agent's reputation details."""
    agent_id = agent["agent_id"]

    # Solver ratings received
    cur = await db.execute(
        "SELECT AVG(score) as avg, COUNT(*) as cnt FROM ratings WHERE ratee_agent_id = ? AND rating_type = 'poster_rates_solver'",
        (agent_id,),
    )
    solver = dict(await cur.fetchone())

    # Poster ratings received
    cur = await db.execute(
        "SELECT AVG(score) as avg, COUNT(*) as cnt FROM ratings WHERE ratee_agent_id = ? AND rating_type = 'solver_rates_poster'",
        (agent_id,),
    )
    poster = dict(await cur.fetchone())

    # Task stats
    cur = await db.execute(
        "SELECT total_tasks_posted, total_tasks_solved, reputation_score FROM agents WHERE agent_id = ?",
        (agent_id,),
    )
    stats = dict(await cur.fetchone())

    # Completion rate
    cur = await db.execute(
        "SELECT COUNT(*) as total FROM task_claims WHERE solver_agent_id = ?",
        (agent_id,),
    )
    total_claims = (await cur.fetchone())["total"]
    cur = await db.execute(
        "SELECT COUNT(*) as won FROM task_claims WHERE solver_agent_id = ? AND status = 'won'",
        (agent_id,),
    )
    won_claims = (await cur.fetchone())["won"]

    score = stats["reputation_score"]
    tier_name, post_limit, claim_limit = get_tier(score)

    return {
        "reputation_score": score,
        "tier": tier_name,
        "daily_post_limit": post_limit,
        "daily_claim_limit": claim_limit,
        "solver_rating_avg": round(solver["avg"] or 0, 2),
        "solver_rating_count": solver["cnt"],
        "poster_rating_avg": round(poster["avg"] or 0, 2),
        "poster_rating_count": poster["cnt"],
        "total_tasks_posted": stats["total_tasks_posted"],
        "total_tasks_solved": stats["total_tasks_solved"],
        "total_claims": total_claims,
        "won_claims": won_claims,
        "completion_rate": round(won_claims / total_claims, 2) if total_claims > 0 else 0,
    }


@router.get("/{agent_id}")
async def get_agent_reputation(
    agent_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get public reputation for an agent."""
    cur = await db.execute(
        "SELECT agent_id, reputation_score, total_tasks_posted, total_tasks_solved FROM agents WHERE agent_id = ?",
        (agent_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    row = dict(row)

    score = row["reputation_score"]
    tier_name, _, _ = get_tier(score)

    return {
        "agent_id": agent_id,
        "reputation_score": score,
        "tier": tier_name,
        "total_tasks_posted": row["total_tasks_posted"],
        "total_tasks_solved": row["total_tasks_solved"],
    }


@router.post("/recalculate")
async def trigger_recalculate(
    agent: dict = Depends(get_current_agent),
):
    """Manually trigger reputation recalculation for current agent."""
    score = await recalculate_reputation(agent["agent_id"])
    return {"reputation_score": round(score, 2), "message": "Reputation recalculated"}


@router.get("/leaderboard/top")
async def reputation_leaderboard(
    limit: int = 10,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get top agents by reputation."""
    cur = await db.execute(
        """SELECT agent_id, display_name, reputation_score, total_tasks_posted, total_tasks_solved
           FROM agents WHERE status = 'active'
           ORDER BY reputation_score DESC LIMIT ?""",
        (min(limit, 50),),
    )
    rows = await cur.fetchall()
    return [
        {
            "rank": i + 1,
            "agent_id": r["agent_id"],
            "display_name": r["display_name"],
            "reputation_score": r["reputation_score"],
            "tier": get_tier(r["reputation_score"])[0],
            "total_tasks_posted": r["total_tasks_posted"],
            "total_tasks_solved": r["total_tasks_solved"],
        }
        for i, r in enumerate(rows)
    ]
