"""Task lifecycle state machine."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from app.config import config
from app.models.schemas import shl_to_micro
from app.services import wallet_service


async def create_task(db: aiosqlite.Connection, poster_agent_id: str,
                      title: str, description: str, bounty_shl: int,
                      category: str = "general", tags: list[str] | None = None,
                      difficulty: str = "medium", estimated_self_cost_shl: int | None = None,
                      max_solvers: int = 5, deadline_hours: int | None = None,
                      context: dict | None = None) -> dict:
    """Create a bounty task and lock funds."""
    # Check reputation
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (poster_agent_id,)
    )
    agent = await cur.fetchone()
    if agent and agent["reputation_score"] < config.reputation_ban_threshold:
        raise ValueError("Reputation too low to post tasks")

    # Lock bounty
    await wallet_service.lock_bounty(db, poster_agent_id, bounty_shl, "pending")

    task_id = str(uuid.uuid4())
    hours = deadline_hours or config.task_default_deadline_hours
    deadline = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()

    await db.execute(
        """INSERT INTO tasks (task_id, poster_agent_id, title, description, category, tags,
           difficulty, bounty_amount, estimated_self_cost, max_solvers, deadline, context)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (task_id, poster_agent_id, title, description, category,
         json.dumps(tags or []), difficulty, shl_to_micro(bounty_shl),
         shl_to_micro(estimated_self_cost_shl) if estimated_self_cost_shl else None,
         max_solvers, deadline, json.dumps(context or {})),
    )

    # Update the lock transaction reference
    await db.execute(
        "UPDATE transactions SET reference_id = ? WHERE reference_id = 'pending' AND tx_type = 'bounty_lock'",
        (task_id,),
    )

    # Increment poster count
    await db.execute(
        "UPDATE agents SET total_tasks_posted = total_tasks_posted + 1 WHERE agent_id = ?",
        (poster_agent_id,),
    )

    await db.commit()

    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    return dict(await cur.fetchone())


async def get_task(db: aiosqlite.Connection, task_id: str) -> dict | None:
    """Get task by ID."""
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_tasks(db: aiosqlite.Connection, status: str | None = None,
                     category: str | None = None, difficulty: str | None = None,
                     page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """List tasks with optional filters."""
    conditions = []
    params = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if difficulty:
        conditions.append("difficulty = ?")
        params.append(difficulty)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Count
    cur = await db.execute(f"SELECT COUNT(*) as cnt FROM tasks{where}", params)
    total = (await cur.fetchone())["cnt"]

    # Fetch
    offset = (page - 1) * page_size
    cur = await db.execute(
        f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows], total


async def claim_task(db: aiosqlite.Connection, task_id: str, solver_agent_id: str) -> dict:
    """Claim a task. Returns claim dict."""
    task = await get_task(db, task_id)
    if not task:
        raise ValueError("Task not found")
    if task["status"] not in ("open", "claimed"):
        raise ValueError(f"Task cannot be claimed (status: {task['status']})")
    if task["poster_agent_id"] == solver_agent_id:
        raise ValueError("Cannot claim your own task")

    # Check existing claim
    cur = await db.execute(
        "SELECT * FROM task_claims WHERE task_id = ? AND solver_agent_id = ? AND status = 'active'",
        (task_id, solver_agent_id),
    )
    if await cur.fetchone():
        raise ValueError("Already claimed this task")

    # Check max solvers
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM task_claims WHERE task_id = ? AND status IN ('active','submitted')",
        (task_id,),
    )
    count = (await cur.fetchone())["cnt"]
    if count >= task["max_solvers"]:
        raise ValueError("Maximum solvers reached")

    # Lock claim deposit
    deposit_tx = await wallet_service.lock_claim_deposit(db, solver_agent_id, task_id, config.claim_deposit_shl)

    claim_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO task_claims (claim_id, task_id, solver_agent_id, deposit_tx_id) VALUES (?, ?, ?, ?)",
        (claim_id, task_id, solver_agent_id, deposit_tx),
    )

    # Update task status to claimed
    if task["status"] == "open":
        await db.execute(
            "UPDATE tasks SET status = 'claimed', updated_at = datetime('now') WHERE task_id = ?",
            (task_id,),
        )

    await db.commit()
    cur = await db.execute("SELECT * FROM task_claims WHERE claim_id = ?", (claim_id,))
    return dict(await cur.fetchone())


async def cancel_task(db: aiosqlite.Connection, task_id: str, poster_agent_id: str) -> dict:
    """Cancel a task. Refund with optional fee."""
    task = await get_task(db, task_id)
    if not task:
        raise ValueError("Task not found")
    if task["poster_agent_id"] != poster_agent_id:
        raise ValueError("Only poster can cancel")
    if task["status"] in ("completed", "cancelled"):
        raise ValueError(f"Task cannot be cancelled (status: {task['status']})")

    # Check if anyone claimed
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM task_claims WHERE task_id = ? AND status IN ('active','submitted')",
        (task_id,),
    )
    has_claims = (await cur.fetchone())["cnt"] > 0
    fee_pct = config.cancel_fee_pct if has_claims else 0

    bounty_shl = task["bounty_amount"] // 1_000_000
    await wallet_service.refund_bounty(db, poster_agent_id, bounty_shl, task_id, fee_pct)

    # Cancel all active claims and refund deposits
    cur = await db.execute(
        "SELECT * FROM task_claims WHERE task_id = ? AND status = 'active'",
        (task_id,),
    )
    claims = await cur.fetchall()
    for claim in claims:
        await wallet_service.refund_claim_deposit(db, claim["solver_agent_id"], task_id, config.claim_deposit_shl)
        await db.execute(
            "UPDATE task_claims SET status = 'withdrawn', updated_at = datetime('now') WHERE claim_id = ?",
            (claim["claim_id"],),
        )

    await db.execute(
        "UPDATE tasks SET status = 'cancelled', updated_at = datetime('now') WHERE task_id = ?",
        (task_id,),
    )
    await db.commit()

    return await get_task(db, task_id)
