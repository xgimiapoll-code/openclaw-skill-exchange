"""Submission service — winner selection and task completion logic."""

import json
import logging
import uuid

import aiosqlite

from app.config import config
from app.services import wallet_service, skill_service
from app.services.event_bus import event_bus, Event

logger = logging.getLogger(__name__)


async def complete_task_with_winner(
    db: aiosqlite.Connection,
    task: dict,
    submission: dict,
    poster_agent_id: str,
    feedback: str | None,
    rating: int,
) -> dict:
    """Complete a task by selecting a winning submission.

    Handles: bounty release, status updates, skill creation, rating record, stat updates.
    Caller is responsible for db.commit().

    Returns dict with result details.
    """
    task_id = task["task_id"]
    bounty_shl = task["bounty_amount"] // 1_000_000

    # Release bounty to solver (includes Master bonus check)
    release_tx, bonus_tx = await wallet_service.release_bounty(
        db, poster_agent_id, submission["solver_agent_id"],
        bounty_shl, task_id, config.bounty_winner_bonus_pct,
    )

    # Update submission
    await db.execute(
        """UPDATE submissions SET status = 'accepted', poster_feedback = ?, poster_rating = ?
           WHERE submission_id = ?""",
        (feedback, rating, submission["submission_id"]),
    )

    # Update winning claim
    await db.execute(
        "UPDATE task_claims SET status = 'won', updated_at = datetime('now') WHERE claim_id = ?",
        (submission["claim_id"],),
    )

    # Mark other claims as lost
    await db.execute(
        """UPDATE task_claims SET status = 'lost', updated_at = datetime('now')
           WHERE task_id = ? AND claim_id != ? AND status IN ('active', 'submitted')""",
        (task_id, submission["claim_id"]),
    )

    # Reject other submissions
    await db.execute(
        """UPDATE submissions SET status = 'rejected'
           WHERE task_id = ? AND submission_id != ? AND status = 'pending'""",
        (task_id, submission["submission_id"]),
    )

    # Complete task
    await db.execute(
        """UPDATE tasks SET status = 'completed', winning_submission_id = ?,
           updated_at = datetime('now') WHERE task_id = ?""",
        (submission["submission_id"], task_id),
    )

    # Update solver stats
    await db.execute(
        "UPDATE agents SET total_tasks_solved = total_tasks_solved + 1 WHERE agent_id = ?",
        (submission["solver_agent_id"],),
    )

    # Auto-create skill from recipe if provided
    recipe = submission.get("skill_recipe", "{}")
    if isinstance(recipe, str):
        recipe = json.loads(recipe)

    skill_id = None
    if recipe and recipe.get("metadata", {}).get("name"):
        meta = recipe["metadata"]
        try:
            skill = await skill_service.create_skill(
                db,
                author_agent_id=submission["solver_agent_id"],
                name=meta["name"],
                title=meta.get("title", meta["name"]),
                description=meta.get("description"),
                category=meta.get("category", "general"),
                tags=meta.get("tags", []),
                recipe=recipe,
                source_task_id=task_id,
            )
            skill_id = skill["skill_id"]
            # Auto-install for poster
            await skill_service.install_skill(db, poster_agent_id, skill_id)
        except Exception as e:
            logger.warning("Best-effort skill creation failed for task %s: %s", task_id, e)

    # Create rating record
    rating_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO ratings (rating_id, task_id, rater_agent_id, ratee_agent_id,
           rating_type, score, comment)
           VALUES (?, ?, ?, ?, 'poster_rates_solver', ?, ?)""",
        (rating_id, task_id, poster_agent_id, submission["solver_agent_id"],
         rating, feedback),
    )

    # Calculate actual bonus (may include Master bonus)
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (submission["solver_agent_id"],)
    )
    solver_agent = await cur.fetchone()
    effective_bonus_pct = config.bounty_winner_bonus_pct
    if solver_agent and solver_agent["reputation_score"] >= config.master_reputation_threshold:
        effective_bonus_pct += config.master_bonus_pct

    result = {
        "task_id": task_id,
        "winning_submission_id": submission["submission_id"],
        "solver_agent_id": submission["solver_agent_id"],
        "bounty_released_shl": bounty_shl,
        "bonus_shl": bounty_shl * effective_bonus_pct // 100,
        "skill_id": skill_id,
        "release_tx_id": release_tx,
        "bonus_tx_id": bonus_tx,
    }

    # If this is a subtask, check if parent task is fully complete
    if task.get("task_type") == "subtask" and task.get("parent_task_id"):
        from app.services.collaboration_service import check_and_release_parent
        release = await check_and_release_parent(db, task["parent_task_id"])
        if release:
            result["parent_release"] = release

    try:
        await event_bus.publish(Event(
            topic="task.completed",
            data={"task_id": task_id, "solver_agent_id": submission["solver_agent_id"], "bounty_shl": bounty_shl},
            target_agent_ids=[poster_agent_id, submission["solver_agent_id"]],
        ))
    except Exception as e:
        logger.debug("Event publish failed: %s", e)

    return result
