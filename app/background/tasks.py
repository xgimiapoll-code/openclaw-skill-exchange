"""Background tasks — expired task cleanup, reputation recalculation."""

import asyncio
import logging

from app.db import get_db_ctx
from app.services import wallet_service

logger = logging.getLogger(__name__)


async def expire_overdue_tasks():
    """Find tasks past deadline and expire them, refunding bounties."""
    async with get_db_ctx() as db:
        cur = await db.execute(
            """SELECT task_id, poster_agent_id, bounty_amount, status
               FROM tasks
               WHERE status IN ('open', 'claimed')
               AND deadline < datetime('now')""",
        )
        tasks = await cur.fetchall()

        for task in tasks:
            task = dict(task)
            bounty_shl = task["bounty_amount"] // 1_000_000

            # Check if there are active claims
            cur2 = await db.execute(
                "SELECT COUNT(*) as cnt FROM task_claims WHERE task_id = ? AND status = 'active'",
                (task["task_id"],),
            )
            has_claims = (await cur2.fetchone())["cnt"] > 0

            # Refund bounty (no fee for expiration)
            await wallet_service.refund_bounty(
                db, task["poster_agent_id"], bounty_shl, task["task_id"], fee_pct=0
            )

            # Refund claim deposits
            cur3 = await db.execute(
                "SELECT solver_agent_id FROM task_claims WHERE task_id = ? AND status = 'active'",
                (task["task_id"],),
            )
            claims = await cur3.fetchall()
            for claim in claims:
                await wallet_service.refund_claim_deposit(
                    db, claim["solver_agent_id"], task["task_id"], 1
                )
                await db.execute(
                    "UPDATE task_claims SET status = 'withdrawn', updated_at = datetime('now') WHERE task_id = ? AND solver_agent_id = ?",
                    (task["task_id"], claim["solver_agent_id"]),
                )

            await db.execute(
                "UPDATE tasks SET status = 'expired', updated_at = datetime('now') WHERE task_id = ?",
                (task["task_id"],),
            )
            logger.info("Expired task %s (bounty: %d SHL)", task["task_id"], bounty_shl)

        if tasks:
            await db.commit()
            logger.info("Expired %d overdue tasks", len(tasks))


async def recalculate_reputation(agent_id: str):
    """Recalculate reputation score for an agent based on ratings and activity."""
    try:
        return await _recalculate_reputation_inner(agent_id)
    except Exception as e:
        logger.debug("Reputation recalculation skipped: %s", e)
        return 0.0


async def _recalculate_reputation_inner(agent_id: str):
    async with get_db_ctx() as db:
        # Solver ratings (how others rate this agent as solver)
        cur = await db.execute(
            "SELECT AVG(score) as avg_score, COUNT(*) as cnt FROM ratings WHERE ratee_agent_id = ? AND rating_type = 'poster_rates_solver'",
            (agent_id,),
        )
        solver_row = dict(await cur.fetchone())
        solver_avg = solver_row["avg_score"] or 0
        solver_cnt = solver_row["cnt"] or 0

        # Poster ratings (how solvers rate this agent as poster)
        cur = await db.execute(
            "SELECT AVG(score) as avg_score, COUNT(*) as cnt FROM ratings WHERE ratee_agent_id = ? AND rating_type = 'solver_rates_poster'",
            (agent_id,),
        )
        poster_row = dict(await cur.fetchone())
        poster_avg = poster_row["avg_score"] or 0

        # Completion rate
        cur = await db.execute(
            "SELECT total_tasks_solved, total_tasks_posted FROM agents WHERE agent_id = ?",
            (agent_id,),
        )
        agent = dict(await cur.fetchone())

        # Claims made vs completed
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

        completion_rate = (won_claims / total_claims * 5) if total_claims > 0 else 2.5

        # Activity score (based on recent activity)
        activity = min(5.0, (agent["total_tasks_posted"] + agent["total_tasks_solved"]) * 0.5)

        # Formula: weighted average, scaled to 0-100
        reputation = (
            0.35 * solver_avg +
            0.20 * poster_avg +
            0.15 * completion_rate +
            0.10 * min(5.0, solver_cnt * 0.5) +  # skill quality proxy
            0.10 * activity +
            0.10 * 2.5  # dispute score (neutral, no disputes implemented)
        ) * 20  # scale 0-5 avg to 0-100

        await db.execute(
            "UPDATE agents SET reputation_score = ?, updated_at = datetime('now') WHERE agent_id = ?",
            (round(reputation, 2), agent_id),
        )
        await db.commit()
        return reputation


async def cleanup_loop(interval_seconds: int = 300):
    """Run periodic cleanup tasks."""
    while True:
        try:
            await expire_overdue_tasks()
        except Exception as e:
            logger.error("Error in cleanup loop: %s", e)
        await asyncio.sleep(interval_seconds)
