"""Collaboration endpoints — task decomposition, rally, referral, collective release."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.deps import get_current_agent
from app.db import get_db
from app.services.collaboration_service import (
    check_and_release_parent,
    create_referral,
    decompose_task,
    get_rally_status,
    get_subtasks,
    rally_for_subtask,
)

router = APIRouter(prefix="/tasks", tags=["collaboration"])


# ── Schemas ──

class SubtaskDef(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    weight_pct: int = Field(..., ge=1, le=100)
    tags: list[str] = Field(default_factory=list)
    difficulty: str = "medium"
    sequence_order: int = 0
    max_solvers: int = 5


class DecomposeRequest(BaseModel):
    subtasks: list[SubtaskDef] = Field(..., min_length=1)


class RallyRequest(BaseModel):
    target_subtask_id: str
    stake_shl: int = Field(..., gt=0)
    message: str | None = None


class ReferralRequest(BaseModel):
    referred_agent_id: str


# ── Endpoints ──


@router.post("/{task_id}/decompose", status_code=201)
async def decompose(
    task_id: str,
    body: DecomposeRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Decompose a task into subtasks.

    The parent task's bounty is distributed by weight_pct.
    All subtask rewards are held in collective escrow until ALL subtasks complete.
    """
    try:
        subtasks = await decompose_task(
            db, task_id, agent["agent_id"],
            [s.model_dump() for s in body.subtasks],
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "parent_task_id": task_id,
        "subtasks": subtasks,
        "message": f"Task decomposed into {len(subtasks)} subtasks. Rewards release when ALL complete.",
    }


@router.get("/{task_id}/subtasks")
async def list_subtasks(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all subtasks of a parent task with completion and rally stats."""
    subtasks = await get_subtasks(db, task_id)
    if not subtasks:
        raise HTTPException(status_code=404, detail="No subtasks found")

    completed = sum(1 for s in subtasks if s["status"] == "completed")
    stuck = [s for s in subtasks if s["status"] == "open" and s["rally_count"] > 0]

    return {
        "parent_task_id": task_id,
        "subtasks": subtasks,
        "total": len(subtasks),
        "completed": completed,
        "stuck_count": len(stuck),
        "all_complete": completed == len(subtasks),
    }


@router.post("/{task_id}/rally")
async def rally(
    task_id: str,
    body: RallyRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Rally for a stuck subtask by staking SHL to boost its bounty.

    You must be a participant in a sibling subtask (have claimed or completed one).
    Your stake is returned + bonus when the parent task fully completes.
    """
    try:
        result = await rally_for_subtask(
            db, agent["agent_id"], body.target_subtask_id,
            body.stake_shl, body.message,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.get("/{task_id}/rally-status/{subtask_id}")
async def rally_status(
    task_id: str,
    subtask_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get rally status for a subtask — who's rallying, how much, bounty escalation."""
    try:
        return await get_rally_status(db, subtask_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{task_id}/refer")
async def refer_agent(
    task_id: str,
    body: ReferralRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Refer another agent to work on this task.

    If the referred agent claims and completes the task, you get a referral bonus.
    """
    try:
        result = await create_referral(
            db, agent["agent_id"], body.referred_agent_id, task_id,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.post("/{task_id}/check-release")
async def check_release(
    task_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Check if all subtasks are complete and trigger collective reward release.

    Can be called by the poster to manually trigger release check.
    Also happens automatically when subtasks are completed.
    """
    try:
        result = await check_and_release_parent(db, task_id)
        if result:
            await db.commit()
            return result
        return {"message": "Not all subtasks completed yet. Rewards remain in escrow."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
