"""Task bounty management endpoints."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import get_current_agent
from app.db import get_db
from app.models.schemas import TaskCreate, TaskListOut, TaskOut
from app.services import task_engine

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(
    body: TaskCreate,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Post a new bounty task. Locks SHL from wallet."""
    try:
        task = await task_engine.create_task(
            db,
            poster_agent_id=agent["agent_id"],
            title=body.title,
            description=body.description,
            bounty_shl=body.bounty_shl,
            category=body.category,
            tags=body.tags,
            difficulty=body.difficulty,
            estimated_self_cost_shl=body.estimated_self_cost_shl,
            max_solvers=body.max_solvers,
            deadline_hours=body.deadline_hours,
            context=body.context,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TaskOut.from_row(task)


@router.get("", response_model=TaskListOut)
async def list_tasks(
    status: str | None = None,
    category: str | None = None,
    difficulty: str | None = None,
    tag: str | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Browse available tasks with optional filters."""
    tasks, total = await task_engine.list_tasks(db, status, category, difficulty, tag, search, page, page_size)

    task_outs = []
    for t in tasks:
        # Get claim and submission counts
        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM task_claims WHERE task_id = ? AND status IN ('active','submitted')",
            (t["task_id"],),
        )
        claim_count = (await cur.fetchone())["cnt"]

        cur = await db.execute(
            "SELECT COUNT(*) as cnt FROM submissions WHERE task_id = ?",
            (t["task_id"],),
        )
        sub_count = (await cur.fetchone())["cnt"]

        task_outs.append(TaskOut.from_row(t, claim_count, sub_count))

    return TaskListOut(tasks=task_outs, total=total, page=page, page_size=page_size)


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get task details."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM task_claims WHERE task_id = ? AND status IN ('active','submitted')",
        (task_id,),
    )
    claim_count = (await cur.fetchone())["cnt"]

    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM submissions WHERE task_id = ?",
        (task_id,),
    )
    sub_count = (await cur.fetchone())["cnt"]

    return TaskOut.from_row(task, claim_count, sub_count)


@router.delete("/{task_id}", response_model=TaskOut)
async def cancel_task(
    task_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Cancel a task. Full refund if no claims, 95% if claimed."""
    try:
        task = await task_engine.cancel_task(db, task_id, agent["agent_id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return TaskOut.from_row(task)


@router.post("/{task_id}/claim")
async def claim_task(
    task_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Claim a task to work on. Locks 1 SHL deposit."""
    try:
        claim = await task_engine.claim_task(db, task_id, agent["agent_id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "claim_id": claim["claim_id"],
        "task_id": claim["task_id"],
        "solver_agent_id": claim["solver_agent_id"],
        "status": claim["status"],
        "created_at": claim.get("created_at", ""),
    }
