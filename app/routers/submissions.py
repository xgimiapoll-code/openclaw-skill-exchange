"""Submission and winner selection endpoints."""

import asyncio
import json
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_agent
from app.background.tasks import recalculate_reputation
from app.config import config
from app.db import get_db
from app.models.schemas import (
    RateRequest,
    SelectWinnerRequest,
    SubmissionCreate,
    SubmissionOut,
)
from app.services import task_engine, wallet_service, skill_service

router = APIRouter(prefix="/tasks", tags=["submissions"])


@router.post("/{task_id}/submissions", response_model=SubmissionOut, status_code=201)
async def create_submission(
    task_id: str,
    body: SubmissionCreate,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Submit a solution for a claimed task."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in ("claimed", "in_review"):
        raise HTTPException(status_code=400, detail=f"Task not accepting submissions (status: {task['status']})")

    # Verify solver has an active claim
    cur = await db.execute(
        "SELECT * FROM task_claims WHERE task_id = ? AND solver_agent_id = ? AND status = 'active'",
        (task_id, agent["agent_id"]),
    )
    claim = await cur.fetchone()
    if not claim:
        raise HTTPException(status_code=403, detail="No active claim on this task")

    claim = dict(claim)

    # Create submission
    submission_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO submissions (submission_id, task_id, claim_id, solver_agent_id,
           skill_recipe, summary, confidence_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (submission_id, task_id, claim["claim_id"], agent["agent_id"],
         json.dumps(body.skill_recipe), body.summary, body.confidence_score),
    )

    # Update claim status
    await db.execute(
        "UPDATE task_claims SET status = 'submitted', updated_at = datetime('now') WHERE claim_id = ?",
        (claim["claim_id"],),
    )

    # Refund claim deposit
    await wallet_service.refund_claim_deposit(
        db, agent["agent_id"], task_id, config.claim_deposit_shl
    )

    # Move task to in_review
    await db.execute(
        "UPDATE tasks SET status = 'in_review', updated_at = datetime('now') WHERE task_id = ?",
        (task_id,),
    )

    await db.commit()

    cur = await db.execute("SELECT * FROM submissions WHERE submission_id = ?", (submission_id,))
    return SubmissionOut.from_row(dict(await cur.fetchone()))


@router.get("/{task_id}/submissions", response_model=list[SubmissionOut])
async def list_submissions(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all submissions for a task."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    cur = await db.execute(
        "SELECT * FROM submissions WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    )
    rows = await cur.fetchall()
    return [SubmissionOut.from_row(dict(r)) for r in rows]


@router.post("/{task_id}/select-winner")
async def select_winner(
    task_id: str,
    body: SelectWinnerRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Select winning submission. Releases bounty + bonus to solver."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["poster_agent_id"] != agent["agent_id"]:
        raise HTTPException(status_code=403, detail="Only poster can select winner")
    if task["status"] != "in_review":
        raise HTTPException(status_code=400, detail=f"Task not in review (status: {task['status']})")

    # Verify submission exists and belongs to this task
    cur = await db.execute(
        "SELECT * FROM submissions WHERE submission_id = ? AND task_id = ?",
        (body.submission_id, task_id),
    )
    submission = await cur.fetchone()
    if not submission:
        raise HTTPException(status_code=404, detail="Submission not found")
    submission = dict(submission)

    bounty_shl = task["bounty_amount"] // 1_000_000

    # Release bounty to solver
    release_tx, bonus_tx = await wallet_service.release_bounty(
        db, agent["agent_id"], submission["solver_agent_id"],
        bounty_shl, task_id, config.bounty_winner_bonus_pct,
    )

    # Update submission
    await db.execute(
        """UPDATE submissions SET status = 'accepted', poster_feedback = ?, poster_rating = ?
           WHERE submission_id = ?""",
        (body.feedback, body.rating, body.submission_id),
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
        (task_id, body.submission_id),
    )

    # Complete task
    await db.execute(
        """UPDATE tasks SET status = 'completed', winning_submission_id = ?,
           updated_at = datetime('now') WHERE task_id = ?""",
        (body.submission_id, task_id),
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
            await skill_service.install_skill(db, agent["agent_id"], skill_id)
        except Exception:
            pass  # Skill creation is best-effort

    # Create rating record
    rating_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO ratings (rating_id, task_id, rater_agent_id, ratee_agent_id,
           rating_type, score, comment)
           VALUES (?, ?, ?, ?, 'poster_rates_solver', ?, ?)""",
        (rating_id, task_id, agent["agent_id"], submission["solver_agent_id"],
         body.rating, body.feedback),
    )

    await db.commit()

    # Recalculate reputation in background
    asyncio.create_task(recalculate_reputation(submission["solver_agent_id"]))
    asyncio.create_task(recalculate_reputation(agent["agent_id"]))

    return {
        "task_id": task_id,
        "winning_submission_id": body.submission_id,
        "solver_agent_id": submission["solver_agent_id"],
        "bounty_released_shl": bounty_shl,
        "bonus_shl": bounty_shl * config.bounty_winner_bonus_pct // 100,
        "skill_id": skill_id,
        "release_tx_id": release_tx,
        "bonus_tx_id": bonus_tx,
    }


@router.post("/{task_id}/rate")
async def rate_task(
    task_id: str,
    body: RateRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Rate the other party after task completion (solver rates poster)."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] != "completed":
        raise HTTPException(status_code=400, detail="Task not completed")

    # Determine rating type
    if agent["agent_id"] == task["poster_agent_id"]:
        raise HTTPException(status_code=400, detail="Use select-winner to rate as poster")

    # Verify agent was solver
    cur = await db.execute(
        "SELECT * FROM task_claims WHERE task_id = ? AND solver_agent_id = ? AND status = 'won'",
        (task_id, agent["agent_id"]),
    )
    if not await cur.fetchone():
        raise HTTPException(status_code=403, detail="Only winning solver can rate poster")

    # Check duplicate rating
    cur = await db.execute(
        "SELECT * FROM ratings WHERE task_id = ? AND rater_agent_id = ? AND rating_type = 'solver_rates_poster'",
        (task_id, agent["agent_id"]),
    )
    if await cur.fetchone():
        raise HTTPException(status_code=409, detail="Already rated")

    rating_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO ratings (rating_id, task_id, rater_agent_id, ratee_agent_id,
           rating_type, score, comment)
           VALUES (?, ?, ?, ?, 'solver_rates_poster', ?, ?)""",
        (rating_id, task_id, agent["agent_id"], task["poster_agent_id"],
         body.score, body.comment),
    )
    await db.commit()

    # Recalculate poster reputation
    asyncio.create_task(recalculate_reputation(task["poster_agent_id"]))

    return {"rating_id": rating_id, "score": body.score, "message": "Rating submitted"}
