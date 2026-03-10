"""Task lifecycle state machine."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from app.config import config
from app.models.schemas import shl_to_micro
from app.services import wallet_service
from app.services.event_bus import event_bus, Event
from app.services.rate_limiter import check_daily_limit


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

    # Check daily rate limit
    allowed, reason = await check_daily_limit(db, poster_agent_id, "post")
    if not allowed:
        raise ValueError(reason)

    # Generate task_id first to avoid "pending" placeholder race condition
    task_id = str(uuid.uuid4())

    # Lock bounty with real task_id
    await wallet_service.lock_bounty(db, poster_agent_id, bounty_shl, task_id)

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

    # Increment poster count
    await db.execute(
        "UPDATE agents SET total_tasks_posted = total_tasks_posted + 1 WHERE agent_id = ?",
        (poster_agent_id,),
    )

    await db.commit()

    try:
        await event_bus.publish(Event(
            topic="task.new",
            data={"task_id": task_id, "title": title, "bounty_shl": bounty_shl, "category": category},
        ))
    except Exception:
        pass

    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    return dict(await cur.fetchone())


async def get_task(db: aiosqlite.Connection, task_id: str) -> dict | None:
    """Get task by ID."""
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_tasks(db: aiosqlite.Connection, status: str | None = None,
                     category: str | None = None, difficulty: str | None = None,
                     tag: str | None = None, search: str | None = None,
                     page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """List tasks with optional filters."""
    conditions = []
    params: list = []

    if status:
        conditions.append("status = ?")
        params.append(status)
    if category:
        conditions.append("category = ?")
        params.append(category)
    if difficulty:
        conditions.append("difficulty = ?")
        params.append(difficulty)
    if tag:
        conditions.append("tags LIKE ?")
        params.append(f"%{tag}%")
    if search:
        conditions.append("(title LIKE ? OR description LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

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

    # Check reputation ban
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (solver_agent_id,)
    )
    agent = await cur.fetchone()
    if agent and agent["reputation_score"] < config.reputation_ban_threshold:
        raise ValueError("Reputation too low to claim tasks")

    # Check daily rate limit
    allowed, reason = await check_daily_limit(db, solver_agent_id, "claim")
    if not allowed:
        raise ValueError(reason)

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

    # Update task status to claimed + record first claim time
    if task["status"] == "open":
        await db.execute(
            """UPDATE tasks SET status = 'claimed',
               first_claimed_at = COALESCE(first_claimed_at, datetime('now')),
               updated_at = datetime('now') WHERE task_id = ?""",
            (task_id,),
        )

    await db.commit()

    try:
        await event_bus.publish(Event(
            topic="task.claimed",
            data={"task_id": task_id, "solver_agent_id": solver_agent_id},
            target_agent_ids=[task["poster_agent_id"]],
        ))
    except Exception:
        pass

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


async def withdraw_claim(db: aiosqlite.Connection, task_id: str, solver_agent_id: str) -> dict:
    """Withdraw an active claim. Refunds deposit. Returns updated claim dict."""
    task = await get_task(db, task_id)
    if not task:
        raise ValueError("Task not found")

    # Find the active claim
    cur = await db.execute(
        "SELECT * FROM task_claims WHERE task_id = ? AND solver_agent_id = ? AND status = 'active'",
        (task_id, solver_agent_id),
    )
    claim = await cur.fetchone()
    if not claim:
        raise ValueError("No active claim to withdraw")
    claim = dict(claim)

    # Refund claim deposit
    await wallet_service.refund_claim_deposit(db, solver_agent_id, task_id, config.claim_deposit_shl)

    # Update claim status + increment failed claim count
    await db.execute(
        "UPDATE task_claims SET status = 'withdrawn', updated_at = datetime('now') WHERE claim_id = ?",
        (claim["claim_id"],),
    )
    await db.execute(
        "UPDATE tasks SET failed_claim_count = failed_claim_count + 1 WHERE task_id = ?",
        (task_id,),
    )

    # Check if this was the last active/submitted claim → revert task to open
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM task_claims WHERE task_id = ? AND status IN ('active', 'submitted')",
        (task_id,),
    )
    remaining = (await cur.fetchone())["cnt"]
    if remaining == 0 and task["status"] == "claimed":
        await db.execute(
            "UPDATE tasks SET status = 'open', updated_at = datetime('now') WHERE task_id = ?",
            (task_id,),
        )

    await db.commit()
    cur = await db.execute("SELECT * FROM task_claims WHERE claim_id = ?", (claim["claim_id"],))
    return dict(await cur.fetchone())
