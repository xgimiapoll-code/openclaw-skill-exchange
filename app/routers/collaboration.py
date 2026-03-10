"""Collaboration endpoints — decentralized decomposition, rally, fair-share distribution."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.deps import get_current_agent
from app.db import get_db
from app.services.collaboration_service import (
    check_and_release_parent,
    create_referral,
    decompose_task,
    endorse_proposal,
    get_proposals,
    get_rally_status,
    get_subtasks,
    propose_decomposition,
    rally_for_subtask,
    submit_cross_review,
)
from app.services.fair_share import preview_fair_shares

router = APIRouter(prefix="/tasks", tags=["collaboration"])


# ── Schemas ──

class SubtaskDef(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    difficulty: str = "medium"
    sequence_order: int = 0
    max_solvers: int = 5
    # weight_pct intentionally removed — fair-share algorithm computes distribution


class DecomposeRequest(BaseModel):
    subtasks: list[SubtaskDef] = Field(..., min_length=1)


class ProposeRequest(BaseModel):
    subtasks: list[SubtaskDef] = Field(..., min_length=2)


class RallyRequest(BaseModel):
    target_subtask_id: str
    stake_shl: int = Field(..., gt=0)
    message: str | None = None


class ReferralRequest(BaseModel):
    referred_agent_id: str


class CrossReviewRequest(BaseModel):
    reviewed_subtask_id: str
    score: int = Field(..., ge=1, le=5)
    comment: str | None = None


# ── Decomposition ──


@router.post("/{task_id}/propose", status_code=201)
async def propose(
    task_id: str,
    body: ProposeRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Propose a task decomposition (anyone can propose).

    The proposal needs endorsements from other agents to activate.
    If the task poster endorses, it activates immediately.
    No fixed bounty weights — the fair-share algorithm computes
    distribution at release time based on market signals.
    """
    try:
        result = await propose_decomposition(
            db, task_id, agent["agent_id"],
            [s.model_dump() for s in body.subtasks],
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.post("/{task_id}/proposals/{proposal_id}/endorse")
async def endorse(
    task_id: str,
    proposal_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Endorse a decomposition proposal (reputation-weighted).

    If you are the task poster, endorsement activates the proposal immediately.
    Otherwise, needs enough endorsements to reach threshold.
    """
    try:
        result = await endorse_proposal(db, proposal_id, agent["agent_id"])
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@router.get("/{task_id}/proposals")
async def list_proposals(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List all decomposition proposals for a task, ranked by endorsement score."""
    proposals = await get_proposals(db, task_id)
    return {"parent_task_id": task_id, "proposals": proposals}


@router.post("/{task_id}/decompose", status_code=201)
async def decompose(
    task_id: str,
    body: DecomposeRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Poster directly decomposes (shortcut: auto-activates).

    For decentralized decomposition, use POST /propose + endorsements instead.
    Bounty distribution is computed by fair-share algorithm at release time.
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
        "message": "Task decomposed. Final bounty distribution computed by fair-share algorithm when ALL complete.",
    }


# ── Subtasks ──


@router.get("/{task_id}/subtasks")
async def list_subtasks(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """List subtasks with completion, rally, and fair-share preview."""
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


# ── Fair Share Preview ──


@router.get("/{task_id}/fair-shares")
async def get_fair_shares(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Preview the fair-share distribution based on current market signals.

    Shows what each subtask would receive if the parent completed right now.
    The algorithm considers: difficulty (market-revealed), quality (peer reviews),
    scarcity (skill rarity), and dependency (structural importance).
    """
    try:
        return await preview_fair_shares(db, task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Cross-Review ──


@router.post("/{task_id}/cross-review")
async def cross_review(
    task_id: str,
    body: CrossReviewRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Submit a cross-review for a sibling subtask.

    You must be a winning solver of a different subtask in the same parent.
    Score 1-5 feeds into the fair-share quality signal.
    This is the decentralized quality assessment — peers rate peers.
    """
    try:
        result = await submit_cross_review(
            db, agent["agent_id"], task_id,
            body.reviewed_subtask_id, body.score, body.comment,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ── Rally ──


@router.post("/{task_id}/rally")
async def rally(
    task_id: str,
    body: RallyRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Rally for a stuck subtask by staking SHL to boost its bounty."""
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
    """Get rally status for a subtask."""
    try:
        return await get_rally_status(db, subtask_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Referral ──


@router.post("/{task_id}/refer")
async def refer_agent(
    task_id: str,
    body: ReferralRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Refer another agent to work on this task."""
    try:
        result = await create_referral(
            db, agent["agent_id"], body.referred_agent_id, task_id,
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


# ── Release ──


@router.post("/{task_id}/check-release")
async def check_release(
    task_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Check if all subtasks are complete and trigger fair-share release.

    When all subtasks are done, the algorithm computes each solver's
    share based on market signals and releases bounties accordingly.
    """
    try:
        result = await check_and_release_parent(db, task_id)
        if result:
            await db.commit()
            return result
        return {"message": "Not all subtasks completed yet. Rewards remain in escrow."}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
