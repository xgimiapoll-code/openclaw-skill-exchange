"""Background tasks — expired task cleanup, reputation recalculation, rewards."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import config
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


async def distribute_weekly_rewards():
    """Grant weekly activity rewards to active agents."""
    async with get_db_ctx() as db:
        # Find agents with recent activity (30 days) who haven't received
        # a reward in the last 7 days
        seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        thirty_days_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

        # Find agents with activity in last 30 days
        cur = await db.execute(
            """SELECT DISTINCT a.agent_id FROM agents a
               WHERE a.status = 'active'
               AND (a.last_activity_reward IS NULL OR a.last_activity_reward < ?)
               AND (
                   EXISTS (SELECT 1 FROM tasks WHERE poster_agent_id = a.agent_id AND created_at > ?)
                   OR EXISTS (SELECT 1 FROM task_claims WHERE solver_agent_id = a.agent_id AND created_at > ?)
                   OR EXISTS (SELECT 1 FROM submissions WHERE solver_agent_id = a.agent_id AND created_at > ?)
                   OR EXISTS (SELECT 1 FROM ratings WHERE rater_agent_id = a.agent_id AND created_at > ?)
               )""",
            (seven_days_ago, thirty_days_ago, thirty_days_ago, thirty_days_ago, thirty_days_ago),
        )
        agents = await cur.fetchall()

        rewarded = 0
        for agent in agents:
            try:
                await wallet_service.grant_activity_reward(
                    db, agent["agent_id"], config.weekly_activity_shl
                )
                await db.execute(
                    "UPDATE agents SET last_activity_reward = datetime('now') WHERE agent_id = ?",
                    (agent["agent_id"],),
                )
                rewarded += 1
            except Exception as e:
                logger.warning("Failed to grant activity reward to %s: %s", agent["agent_id"], e)

        if rewarded:
            await db.commit()
            logger.info("Granted weekly activity rewards to %d agents", rewarded)


async def check_skill_publish_rewards():
    """Check for skills that reached the install threshold but haven't been rewarded."""
    async with get_db_ctx() as db:
        cur = await db.execute(
            """SELECT skill_id, author_agent_id, usage_count FROM skills
               WHERE reward_granted = 0 AND usage_count >= ?""",
            (config.skill_publish_min_installs,),
        )
        skills = await cur.fetchall()

        rewarded = 0
        for skill in skills:
            try:
                await wallet_service.grant_skill_reward(
                    db, skill["author_agent_id"], skill["skill_id"],
                    config.skill_publish_reward_shl,
                )
                await db.execute(
                    "UPDATE skills SET reward_granted = 1 WHERE skill_id = ?",
                    (skill["skill_id"],),
                )
                rewarded += 1
            except Exception as e:
                logger.warning("Failed to grant skill reward for %s: %s", skill["skill_id"], e)

        if rewarded:
            await db.commit()
            logger.info("Granted skill publish rewards for %d skills", rewarded)


async def auto_resolve_disputes():
    """Auto-resolve small disputes that have been open for 72+ hours."""
    async with get_db_ctx() as db:
        cutoff_hours = config.dispute_auto_resolve_hours
        cur = await db.execute(
            """SELECT d.dispute_id, d.task_id, d.initiator_agent_id, d.respondent_agent_id
               FROM disputes d
               JOIN tasks t ON d.task_id = t.task_id
               WHERE d.status = 'open'
               AND d.resolution_method = 'auto'
               AND datetime(d.created_at, '+' || ? || ' hours') < datetime('now')""",
            (cutoff_hours,),
        )
        disputes = await cur.fetchall()

        resolved = 0
        for dispute in disputes:
            dispute = dict(dispute)
            # Auto-resolve: pick side with highest confidence_score submission
            cur2 = await db.execute(
                """SELECT solver_agent_id, MAX(confidence_score) as max_conf
                   FROM submissions WHERE task_id = ?
                   GROUP BY solver_agent_id
                   ORDER BY max_conf DESC LIMIT 1""",
                (dispute["task_id"],),
            )
            top = await cur2.fetchone()

            if top and top["solver_agent_id"] == dispute["initiator_agent_id"]:
                status = "resolved_initiator"
            else:
                status = "resolved_respondent"

            await db.execute(
                """UPDATE disputes SET status = ?, resolved_at = datetime('now')
                   WHERE dispute_id = ?""",
                (status, dispute["dispute_id"]),
            )
            resolved += 1

        if resolved:
            await db.commit()
            logger.info("Auto-resolved %d disputes", resolved)


async def recalculate_reputation(agent_id: str):
    """Recalculate reputation score for an agent based on ratings, activity, and disputes."""
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

        # Dispute score — replaces hardcoded 2.5
        dispute_score = await _calculate_dispute_score(db, agent_id)

        # Skill quality score — based on authored skill ratings
        cur = await db.execute(
            """SELECT AVG(sr.score) as avg FROM skill_ratings sr
               JOIN skills s ON sr.skill_id = s.skill_id
               WHERE s.author_agent_id = ?""",
            (agent_id,),
        )
        skill_row = await cur.fetchone()
        skill_quality = skill_row["avg"] if skill_row and skill_row["avg"] else 2.5

        # Formula: weighted average, scaled to 0-100
        reputation = (
            0.30 * solver_avg +
            0.20 * poster_avg +
            0.15 * completion_rate +
            0.10 * min(5.0, solver_cnt * 0.5) +  # rating count proxy
            0.10 * activity +
            0.10 * dispute_score +
            0.05 * skill_quality
        ) * 20  # scale 0-5 avg to 0-100

        await db.execute(
            "UPDATE agents SET reputation_score = ?, updated_at = datetime('now') WHERE agent_id = ?",
            (round(reputation, 2), agent_id),
        )
        await db.commit()
        return reputation


async def _calculate_dispute_score(db, agent_id: str) -> float:
    """Calculate dispute component of reputation.

    No disputes: 2.5 (neutral)
    Won disputes: +0.5/win, cap at 5.0
    Lost disputes: -1.0/loss, floor at 0.0
    """
    # Disputes where agent won (was initiator and resolved in their favor, or respondent and resolved in their favor)
    cur = await db.execute(
        """SELECT COUNT(*) as cnt FROM disputes
           WHERE (initiator_agent_id = ? AND status = 'resolved_initiator')
           OR (respondent_agent_id = ? AND status = 'resolved_respondent')""",
        (agent_id, agent_id),
    )
    wins = (await cur.fetchone())["cnt"]

    cur = await db.execute(
        """SELECT COUNT(*) as cnt FROM disputes
           WHERE (initiator_agent_id = ? AND status = 'resolved_respondent')
           OR (respondent_agent_id = ? AND status = 'resolved_initiator')""",
        (agent_id, agent_id),
    )
    losses = (await cur.fetchone())["cnt"]

    score = 2.5 + wins * 0.5 - losses * 1.0
    return max(0.0, min(5.0, score))


async def run_settlement():
    """Create settlement batches and process pending bridge withdrawals."""
    from app.blockchain.provider import is_blockchain_enabled
    if not is_blockchain_enabled():
        return

    from app.blockchain.settlement import create_settlement_batch, submit_batch_onchain
    from app.blockchain.bridge import process_pending_withdrawals

    async with get_db_ctx() as db:
        # Process pending withdrawals
        try:
            processed = await process_pending_withdrawals(db)
            if processed:
                logger.info("Processed %d bridge withdrawals", processed)
        except Exception as e:
            logger.error("Error processing withdrawals: %s", e)

        # Create and submit settlement batch
        try:
            batch = await create_settlement_batch(
                db, min_batch_size=config.settlement_min_batch_size
            )
            if batch:
                try:
                    result = await submit_batch_onchain(db, batch["batch_id"])
                    logger.info("Settlement batch %s confirmed: %s",
                                batch["batch_id"], result.get("onchain_tx_hash"))
                except Exception as e:
                    logger.warning("Settlement on-chain submission failed: %s", e)
        except Exception as e:
            logger.error("Error creating settlement batch: %s", e)


async def cleanup_loop(interval_seconds: int = 300):
    """Run periodic cleanup tasks."""
    while True:
        try:
            await expire_overdue_tasks()
            await distribute_weekly_rewards()
            await check_skill_publish_rewards()
            await auto_resolve_disputes()
            await run_settlement()
        except Exception as e:
            logger.error("Error in cleanup loop: %s", e)
        await asyncio.sleep(interval_seconds)
