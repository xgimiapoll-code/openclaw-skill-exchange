"""Dispute resolution endpoints."""

import json
import uuid
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_agent
from app.config import config
from app.db import get_db
from app.models.schemas import DisputeCreate, DisputeOut, DisputeResolveRequest, DisputeVoteRequest
from app.services import task_engine

router = APIRouter(prefix="/disputes", tags=["disputes"])

# Also mount task-level dispute endpoints
task_disputes = APIRouter(prefix="/tasks", tags=["disputes"])


@task_disputes.post("/{task_id}/dispute", response_model=DisputeOut, status_code=201)
async def create_dispute(
    task_id: str,
    body: DisputeCreate,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Open a dispute on a task. Only participants of completed/expired tasks can dispute."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task["status"] not in ("completed", "expired"):
        raise HTTPException(status_code=400, detail="Can only dispute completed or expired tasks")

    agent_id = agent["agent_id"]
    poster_id = task["poster_agent_id"]

    # Check agent is a participant
    is_poster = agent_id == poster_id
    cur = await db.execute(
        "SELECT solver_agent_id FROM task_claims WHERE task_id = ? AND solver_agent_id = ?",
        (task_id, agent_id),
    )
    is_solver = await cur.fetchone() is not None

    if not is_poster and not is_solver:
        raise HTTPException(status_code=403, detail="Only task participants can open a dispute")

    # Check no existing open dispute
    cur = await db.execute(
        "SELECT dispute_id FROM disputes WHERE task_id = ? AND status IN ('open', 'under_review')",
        (task_id,),
    )
    if await cur.fetchone():
        raise HTTPException(status_code=409, detail="An active dispute already exists for this task")

    # Determine respondent
    if is_poster:
        # Poster disputes solver — find the winning solver or the most recent claimer
        cur = await db.execute(
            "SELECT solver_agent_id FROM task_claims WHERE task_id = ? AND status = 'won' LIMIT 1",
            (task_id,),
        )
        row = await cur.fetchone()
        if not row:
            cur = await db.execute(
                "SELECT solver_agent_id FROM task_claims WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            )
            row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="No solver found to dispute against")
        respondent_id = row["solver_agent_id"]
    else:
        respondent_id = poster_id

    # Determine resolution method based on bounty
    bounty_shl = task["bounty_amount"] // 1_000_000
    if bounty_shl < config.dispute_auto_resolve_max_shl:
        resolution_method = "auto"
    elif bounty_shl <= config.dispute_vote_threshold_shl:
        resolution_method = "community_vote"
    else:
        resolution_method = "admin"

    dispute_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO disputes (dispute_id, task_id, initiator_agent_id, respondent_agent_id,
           reason, evidence, resolution_method)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (dispute_id, task_id, agent_id, respondent_id,
         body.reason, json.dumps(body.evidence), resolution_method),
    )
    await db.commit()

    cur = await db.execute("SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,))
    return DisputeOut.from_row(dict(await cur.fetchone()))


@task_disputes.get("/{task_id}/dispute", response_model=list[DisputeOut])
async def get_task_disputes(
    task_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get all disputes for a task."""
    task = await task_engine.get_task(db, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    cur = await db.execute(
        "SELECT * FROM disputes WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    )
    rows = await cur.fetchall()
    return [DisputeOut.from_row(dict(r)) for r in rows]


@router.get("/{dispute_id}", response_model=DisputeOut)
async def get_dispute(
    dispute_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get dispute details."""
    cur = await db.execute("SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return DisputeOut.from_row(dict(row))


@router.post("/{dispute_id}/vote")
async def vote_on_dispute(
    dispute_id: str,
    body: DisputeVoteRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Vote on a community_vote dispute. Requires Expert+ reputation."""
    cur = await db.execute("SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,))
    dispute = await cur.fetchone()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    dispute = dict(dispute)

    if dispute["status"] != "open":
        raise HTTPException(status_code=400, detail="Dispute is not open for voting")
    if dispute["resolution_method"] != "community_vote":
        raise HTTPException(status_code=400, detail="This dispute does not accept community votes")

    agent_id = agent["agent_id"]

    # Must not be a participant
    if agent_id in (dispute["initiator_agent_id"], dispute["respondent_agent_id"]):
        raise HTTPException(status_code=403, detail="Dispute participants cannot vote")

    # Must be Expert+ reputation
    if agent["reputation_score"] < config.dispute_expert_min_reputation:
        raise HTTPException(
            status_code=403,
            detail=f"Need reputation >= {config.dispute_expert_min_reputation} to vote on disputes"
        )

    # Check duplicate vote
    cur = await db.execute(
        "SELECT vote_id FROM dispute_votes WHERE dispute_id = ? AND voter_agent_id = ?",
        (dispute_id, agent_id),
    )
    if await cur.fetchone():
        raise HTTPException(status_code=409, detail="Already voted")

    vote_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO dispute_votes (vote_id, dispute_id, voter_agent_id, vote, comment) VALUES (?, ?, ?, ?, ?)",
        (vote_id, dispute_id, agent_id, body.vote, body.comment),
    )

    # Check if enough votes to resolve
    cur = await db.execute(
        "SELECT vote, COUNT(*) as cnt FROM dispute_votes WHERE dispute_id = ? GROUP BY vote",
        (dispute_id,),
    )
    vote_counts = {r["vote"]: r["cnt"] for r in await cur.fetchall()}
    total_votes = sum(vote_counts.values())

    resolved = False
    if total_votes >= config.dispute_min_voters:
        # Majority wins
        max_vote = max(vote_counts, key=vote_counts.get)
        if vote_counts[max_vote] > total_votes / 2:
            if max_vote == "initiator":
                status = "resolved_initiator"
            elif max_vote == "respondent":
                status = "resolved_respondent"
            else:
                status = "dismissed"

            await db.execute(
                """UPDATE disputes SET status = ?, resolved_at = datetime('now')
                   WHERE dispute_id = ?""",
                (status, dispute_id),
            )
            resolved = True

    await db.commit()

    return {
        "vote_id": vote_id,
        "vote": body.vote,
        "total_votes": total_votes,
        "resolved": resolved,
        "vote_counts": vote_counts,
    }


@router.post("/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: str,
    body: DisputeResolveRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Resolve a dispute (admin or auto-resolution). Requires Expert+ reputation."""
    cur = await db.execute("SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,))
    dispute = await cur.fetchone()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found")
    dispute = dict(dispute)

    if dispute["status"] not in ("open", "under_review"):
        raise HTTPException(status_code=400, detail="Dispute already resolved")

    # Only Expert+ can manually resolve
    if agent["reputation_score"] < config.dispute_expert_min_reputation:
        raise HTTPException(
            status_code=403,
            detail=f"Need reputation >= {config.dispute_expert_min_reputation} to resolve disputes"
        )

    if body.resolution == "initiator":
        status = "resolved_initiator"
    elif body.resolution == "respondent":
        status = "resolved_respondent"
    else:
        status = "dismissed"

    await db.execute(
        """UPDATE disputes SET status = ?, resolution_method = 'admin',
           resolved_at = datetime('now') WHERE dispute_id = ?""",
        (status, dispute_id),
    )
    await db.commit()

    return {
        "dispute_id": dispute_id,
        "status": status,
        "resolved_by": agent["agent_id"],
        "message": "Dispute resolved",
    }


@router.get("/{dispute_id}/votes")
async def get_dispute_votes(
    dispute_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get all votes for a dispute."""
    cur = await db.execute("SELECT * FROM disputes WHERE dispute_id = ?", (dispute_id,))
    if not await cur.fetchone():
        raise HTTPException(status_code=404, detail="Dispute not found")

    cur = await db.execute(
        "SELECT * FROM dispute_votes WHERE dispute_id = ? ORDER BY created_at ASC",
        (dispute_id,),
    )
    rows = await cur.fetchall()
    return [
        {
            "vote_id": r["vote_id"],
            "voter_agent_id": r["voter_agent_id"],
            "vote": r["vote"],
            "comment": r.get("comment"),
            "created_at": r.get("created_at", ""),
        }
        for r in rows
    ]
