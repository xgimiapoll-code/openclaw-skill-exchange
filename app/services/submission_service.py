"""Submission service — winner selection and task completion logic."""

import json
import uuid

import aiosqlite

from app.config import config
from app.services import wallet_service, skill_service


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
        except Exception:
            pass  # Skill creation is best-effort

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

    return {
        "task_id": task_id,
        "winning_submission_id": submission["submission_id"],
        "solver_agent_id": submission["solver_agent_id"],
        "bounty_released_shl": bounty_shl,
        "bonus_shl": bounty_shl * effective_bonus_pct // 100,
        "skill_id": skill_id,
        "release_tx_id": release_tx,
        "bonus_tx_id": bonus_tx,
    }
