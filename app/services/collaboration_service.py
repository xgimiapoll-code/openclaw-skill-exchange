"""Collaboration service — task decomposition, rally, escalation, collective release.

Core mechanism: "Rally" (集结号)
- Parent task is split into subtasks; bounties are held in collective escrow
- Nobody gets paid until ALL subtasks complete
- Completed subtask solvers can "rally" by staking part of their pending reward
  to boost stuck subtasks, creating a viral recruitment effect
- Auto-escalation increases bounty on stuck subtasks over time
- When all subtasks complete, rewards release to everyone simultaneously
"""

import json
import math
import uuid
from datetime import datetime, timezone

import aiosqlite

from app.config import config
from app.models.schemas import shl_to_micro, micro_to_shl
from app.services import wallet_service


# ── Task Decomposition ──


async def decompose_task(
    db: aiosqlite.Connection,
    parent_task_id: str,
    poster_agent_id: str,
    subtasks: list[dict],
) -> list[dict]:
    """Split a parent task into subtasks.

    Each subtask dict: {title, description, weight_pct, tags?, difficulty?, sequence_order?}
    Total weight_pct must be <= 100. Remainder goes to coordinator reserve.

    The parent task's bounty is distributed among subtasks by weight.
    Returns list of created subtask dicts.
    """
    # Validate parent task
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = await cur.fetchone()
    if not parent:
        raise ValueError("Parent task not found")
    parent = dict(parent)

    if parent["poster_agent_id"] != poster_agent_id:
        raise ValueError("Only poster can decompose task")
    if parent["task_type"] == "subtask":
        raise ValueError("Cannot decompose a subtask")
    if parent["status"] != "open":
        raise ValueError("Can only decompose open tasks")

    # Check no existing subtasks
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM tasks WHERE parent_task_id = ?", (parent_task_id,)
    )
    if (await cur.fetchone())["cnt"] > 0:
        raise ValueError("Task already decomposed")

    if not subtasks:
        raise ValueError("At least one subtask required")

    # Validate total weight
    total_weight = sum(s.get("weight_pct", 0) for s in subtasks)
    if total_weight > 100:
        raise ValueError(f"Total weight {total_weight}% exceeds 100%")

    # Mark parent as parent type
    await db.execute(
        "UPDATE tasks SET task_type = 'parent', updated_at = datetime('now') WHERE task_id = ?",
        (parent_task_id,),
    )

    parent_bounty = parent["bounty_amount"]
    created = []

    for i, sub in enumerate(subtasks):
        weight = sub.get("weight_pct", 0)
        sub_bounty = parent_bounty * weight // 100

        if sub_bounty <= 0:
            raise ValueError(f"Subtask '{sub.get('title', '')}' has zero bounty (weight too low)")

        task_id = str(uuid.uuid4())
        tags = json.dumps(sub.get("tags", []))
        difficulty = sub.get("difficulty", parent.get("difficulty", "medium"))
        seq = sub.get("sequence_order", i)

        await db.execute(
            """INSERT INTO tasks (task_id, poster_agent_id, title, description, category,
               tags, difficulty, bounty_amount, base_bounty_amount, status, max_solvers,
               deadline, parent_task_id, task_type, weight_pct, sequence_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 'subtask', ?, ?)""",
            (task_id, poster_agent_id, sub["title"], sub["description"],
             parent.get("category", "general"), tags, difficulty,
             sub_bounty, sub_bounty,  # base_bounty = initial bounty
             sub.get("max_solvers", 5), parent.get("deadline"),
             parent_task_id, weight, seq),
        )

        created.append({
            "task_id": task_id,
            "title": sub["title"],
            "weight_pct": weight,
            "bounty_shl": micro_to_shl(sub_bounty),
            "difficulty": difficulty,
            "sequence_order": seq,
        })

    return created


# ── Rally (呐喊) ──


async def rally_for_subtask(
    db: aiosqlite.Connection,
    supporter_agent_id: str,
    target_subtask_id: str,
    stake_shl: int,
    message: str | None = None,
) -> dict:
    """A participant stakes SHL to boost a stuck subtask's bounty.

    Requirements:
    - Supporter must have completed another subtask in the same parent, OR
      have an active claim on a sibling subtask
    - Target subtask must be open (not yet claimed or completed)
    - Minimum stake: config.rally_min_stake_shl

    The staked amount is added to the target subtask's bounty.
    Supporter gets stake back + bonus when parent task completes.
    """
    if stake_shl < config.rally_min_stake_shl:
        raise ValueError(f"Minimum rally stake is {config.rally_min_stake_shl} SHL")

    # Validate target subtask
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (target_subtask_id,))
    subtask = await cur.fetchone()
    if not subtask:
        raise ValueError("Subtask not found")
    subtask = dict(subtask)

    if subtask["task_type"] != "subtask":
        raise ValueError("Can only rally for subtasks")
    if subtask["status"] not in ("open", "claimed"):
        raise ValueError("Subtask is not available for rally")

    parent_task_id = subtask["parent_task_id"]

    # Check supporter is a participant in a sibling subtask
    cur = await db.execute(
        """SELECT t.task_id FROM tasks t
           JOIN task_claims tc ON t.task_id = tc.task_id
           WHERE t.parent_task_id = ? AND tc.solver_agent_id = ?
           AND tc.status IN ('active', 'submitted', 'won')
           AND t.task_id != ?""",
        (parent_task_id, supporter_agent_id, target_subtask_id),
    )
    sibling_claim = await cur.fetchone()
    if not sibling_claim:
        raise ValueError("Must be a participant in a sibling subtask to rally")

    # Check not already rallied
    cur = await db.execute(
        "SELECT rally_id FROM task_rallies WHERE target_subtask_id = ? AND supporter_agent_id = ?",
        (target_subtask_id, supporter_agent_id),
    )
    if await cur.fetchone():
        raise ValueError("Already rallied for this subtask")

    # Lock the stake
    stake_tx = await wallet_service.lock_rally_stake(
        db, supporter_agent_id, stake_shl, target_subtask_id
    )

    # Create rally record
    rally_id = str(uuid.uuid4())
    stake_micro = shl_to_micro(stake_shl)
    await db.execute(
        """INSERT INTO task_rallies
           (rally_id, parent_task_id, target_subtask_id, supporter_agent_id,
            stake_amount, stake_tx_id, message)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (rally_id, parent_task_id, target_subtask_id, supporter_agent_id,
         stake_micro, stake_tx, message),
    )

    # Increase target subtask bounty by stake amount
    await db.execute(
        "UPDATE tasks SET bounty_amount = bounty_amount + ?, updated_at = datetime('now') WHERE task_id = ?",
        (stake_micro, target_subtask_id),
    )

    # Get updated rally stats
    cur = await db.execute(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(stake_amount), 0) as total FROM task_rallies WHERE target_subtask_id = ?",
        (target_subtask_id,),
    )
    stats = dict(await cur.fetchone())

    return {
        "rally_id": rally_id,
        "target_subtask_id": target_subtask_id,
        "stake_shl": stake_shl,
        "message": message,
        "total_rallies": stats["cnt"],
        "total_staked_shl": micro_to_shl(stats["total"]),
        "new_bounty_shl": micro_to_shl(subtask["bounty_amount"] + stake_micro),
    }


# ── Referral ──


async def create_referral(
    db: aiosqlite.Connection,
    referrer_agent_id: str,
    referred_agent_id: str,
    task_id: str,
) -> dict:
    """Record a referral — referrer recruited referred_agent for a task.

    Referrer gets reward when the referred agent successfully completes the task.
    """
    if referrer_agent_id == referred_agent_id:
        raise ValueError("Cannot refer yourself")

    # Validate task exists and is claimable
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    task = await cur.fetchone()
    if not task:
        raise ValueError("Task not found")
    task = dict(task)

    if task["status"] not in ("open", "claimed"):
        raise ValueError("Task not available for referral")

    # Check not duplicate
    cur = await db.execute(
        "SELECT referral_id FROM task_referrals WHERE task_id = ? AND referrer_agent_id = ? AND referred_agent_id = ?",
        (task_id, referrer_agent_id, referred_agent_id),
    )
    if await cur.fetchone():
        raise ValueError("Referral already exists")

    referral_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO task_referrals (referral_id, task_id, referrer_agent_id, referred_agent_id)
           VALUES (?, ?, ?, ?)""",
        (referral_id, task_id, referrer_agent_id, referred_agent_id),
    )

    return {
        "referral_id": referral_id,
        "task_id": task_id,
        "referrer_agent_id": referrer_agent_id,
        "referred_agent_id": referred_agent_id,
        "status": "pending",
    }


# ── Auto-Escalation ──


async def escalate_stuck_subtasks(db: aiosqlite.Connection) -> int:
    """Auto-escalate bounties on stuck subtasks. Called by background task.

    A subtask is "stuck" if:
    - It's a subtask (task_type = 'subtask')
    - Status is 'open' (nobody claimed it)
    - Created more than escalation_interval_hours ago
    - Escalation level hasn't hit the max

    Bounty increases by escalation_rate_pct each interval.
    Increase is funded by system mint (new SHL into circulation).

    Returns number of escalated subtasks.
    """
    interval_hours = config.escalation_interval_hours
    max_mult = config.escalation_max_multiplier
    rate_pct = config.escalation_rate_pct

    cur = await db.execute(
        """SELECT task_id, bounty_amount, base_bounty_amount, escalation_level
           FROM tasks
           WHERE task_type = 'subtask'
           AND status = 'open'
           AND escalation_level < ?
           AND datetime(updated_at, '+' || ? || ' hours') < datetime('now')""",
        (max_mult, interval_hours),
    )
    subtasks = await cur.fetchall()

    escalated = 0
    for st in subtasks:
        st = dict(st)
        base = st["base_bounty_amount"] or st["bounty_amount"]
        new_level = min(st["escalation_level"] + rate_pct / 100, max_mult)
        new_bounty = int(base * new_level)
        increase = new_bounty - st["bounty_amount"]

        if increase <= 0:
            continue

        # Mint the increase
        await wallet_service.mint_escalation(db, st["task_id"], increase)

        # Update subtask bounty and escalation level
        await db.execute(
            """UPDATE tasks SET bounty_amount = ?, escalation_level = ?,
               updated_at = datetime('now') WHERE task_id = ?""",
            (new_bounty, new_level, st["task_id"]),
        )
        escalated += 1

    if escalated:
        await db.commit()

    return escalated


# ── Collective Release ──


async def check_and_release_parent(db: aiosqlite.Connection, parent_task_id: str) -> dict | None:
    """Check if all subtasks of a parent are completed, and release all rewards.

    Called after each subtask completion. If all subtasks done:
    1. Release each subtask solver's bounty from parent's frozen pool
    2. Return rally stakes + bonus to rally participants
    3. Pay referral rewards
    4. Mark parent as completed

    Returns release summary or None if not all subtasks are done.
    """
    # Get parent
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = await cur.fetchone()
    if not parent:
        return None
    parent = dict(parent)

    if parent["task_type"] != "parent":
        return None
    if parent["status"] == "completed":
        return None

    # Check all subtasks
    cur = await db.execute(
        "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY sequence_order",
        (parent_task_id,),
    )
    subtasks = [dict(r) for r in await cur.fetchall()]

    if not subtasks:
        return None

    # All must be completed
    for st in subtasks:
        if st["status"] != "completed":
            return None

    # === ALL SUBTASKS COMPLETED — RELEASE EVERYTHING ===

    release_details = {
        "parent_task_id": parent_task_id,
        "subtasks_completed": len(subtasks),
        "solver_payouts": [],
        "rally_refunds": [],
        "referral_rewards": [],
    }

    poster_id = parent["poster_agent_id"]

    for st in subtasks:
        # Find the winning submission solver
        if not st.get("winning_submission_id"):
            continue

        cur = await db.execute(
            "SELECT solver_agent_id FROM submissions WHERE submission_id = ?",
            (st["winning_submission_id"],),
        )
        sub_row = await cur.fetchone()
        if not sub_row:
            continue
        solver_id = sub_row["solver_agent_id"]

        # Release bounty from parent's frozen pool to solver
        bounty_shl = st["bounty_amount"] // 1_000_000
        release_tx, bonus_tx = await wallet_service.release_bounty(
            db, poster_id, solver_id, bounty_shl, st["task_id"],
            config.bounty_winner_bonus_pct,
        )

        release_details["solver_payouts"].append({
            "subtask_id": st["task_id"],
            "solver_agent_id": solver_id,
            "bounty_shl": bounty_shl,
        })

    # Refund rally stakes + pay bonus
    cur = await db.execute(
        "SELECT * FROM task_rallies WHERE parent_task_id = ? AND status = 'active'",
        (parent_task_id,),
    )
    rallies = [dict(r) for r in await cur.fetchall()]

    for rally in rallies:
        # Refund stake
        await wallet_service.refund_rally_stake(
            db, rally["supporter_agent_id"], rally["stake_amount"], rally["target_subtask_id"]
        )
        # Pay bonus (% of staked amount, minted)
        bonus = rally["stake_amount"] * config.rally_bonus_pct // 100
        if bonus > 0:
            await wallet_service.grant_rally_bonus(
                db, rally["supporter_agent_id"], bonus, rally["target_subtask_id"]
            )

        await db.execute(
            "UPDATE task_rallies SET status = 'rewarded' WHERE rally_id = ?",
            (rally["rally_id"],),
        )

        release_details["rally_refunds"].append({
            "supporter_agent_id": rally["supporter_agent_id"],
            "stake_refunded_shl": micro_to_shl(rally["stake_amount"]),
            "bonus_shl": micro_to_shl(bonus),
        })

    # Pay referral rewards
    for st in subtasks:
        if not st.get("winning_submission_id"):
            continue

        cur = await db.execute(
            "SELECT solver_agent_id FROM submissions WHERE submission_id = ?",
            (st["winning_submission_id"],),
        )
        sub_row = await cur.fetchone()
        if not sub_row:
            continue
        solver_id = sub_row["solver_agent_id"]

        # Check if anyone referred this solver to this subtask
        cur = await db.execute(
            """SELECT * FROM task_referrals
               WHERE task_id = ? AND referred_agent_id = ? AND status = 'pending'""",
            (st["task_id"], solver_id),
        )
        referrals = [dict(r) for r in await cur.fetchall()]

        for ref in referrals:
            reward = st["bounty_amount"] * config.referral_bonus_pct // 100
            if reward > 0:
                reward_tx = await wallet_service.grant_referral_reward(
                    db, ref["referrer_agent_id"], reward, st["task_id"]
                )
                await db.execute(
                    """UPDATE task_referrals SET status = 'rewarded',
                       reward_amount = ?, reward_tx_id = ?
                       WHERE referral_id = ?""",
                    (reward, reward_tx, ref["referral_id"]),
                )
                release_details["referral_rewards"].append({
                    "referrer_agent_id": ref["referrer_agent_id"],
                    "referred_agent_id": ref["referred_agent_id"],
                    "reward_shl": micro_to_shl(reward),
                })

    # Mark parent as completed
    await db.execute(
        "UPDATE tasks SET status = 'completed', updated_at = datetime('now') WHERE task_id = ?",
        (parent_task_id,),
    )

    return release_details


# ── Query helpers ──


async def get_subtasks(db: aiosqlite.Connection, parent_task_id: str) -> list[dict]:
    """Get all subtasks of a parent task with rally and completion stats."""
    cur = await db.execute(
        """SELECT t.*,
              (SELECT COUNT(*) FROM task_claims WHERE task_id = t.task_id AND status IN ('active','submitted')) as claim_count,
              (SELECT COUNT(*) FROM submissions WHERE task_id = t.task_id) as submission_count,
              (SELECT COUNT(*) FROM task_rallies WHERE target_subtask_id = t.task_id AND status = 'active') as rally_count,
              (SELECT COALESCE(SUM(stake_amount), 0) FROM task_rallies WHERE target_subtask_id = t.task_id AND status = 'active') as rally_total
           FROM tasks t
           WHERE t.parent_task_id = ?
           ORDER BY t.sequence_order, t.created_at""",
        (parent_task_id,),
    )
    rows = await cur.fetchall()
    result = []
    for r in rows:
        r = dict(r)
        tags = r.get("tags", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        result.append({
            "task_id": r["task_id"],
            "title": r["title"],
            "description": r["description"],
            "difficulty": r["difficulty"],
            "weight_pct": r["weight_pct"],
            "bounty_shl": micro_to_shl(r["bounty_amount"]),
            "base_bounty_shl": micro_to_shl(r["base_bounty_amount"] or r["bounty_amount"]),
            "escalation_level": r["escalation_level"],
            "status": r["status"],
            "tags": tags,
            "sequence_order": r["sequence_order"],
            "claim_count": r["claim_count"],
            "submission_count": r["submission_count"],
            "rally_count": r["rally_count"],
            "rally_total_shl": micro_to_shl(r["rally_total"]),
            "winning_submission_id": r.get("winning_submission_id"),
            "created_at": r.get("created_at", ""),
        })
    return result


async def get_rally_status(db: aiosqlite.Connection, subtask_id: str) -> dict:
    """Get rally status for a subtask."""
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (subtask_id,))
    task = await cur.fetchone()
    if not task:
        raise ValueError("Task not found")
    task = dict(task)

    cur = await db.execute(
        """SELECT r.*, a.display_name as supporter_name
           FROM task_rallies r
           JOIN agents a ON r.supporter_agent_id = a.agent_id
           WHERE r.target_subtask_id = ?
           ORDER BY r.created_at""",
        (subtask_id,),
    )
    rallies = []
    for r in await cur.fetchall():
        r = dict(r)
        rallies.append({
            "rally_id": r["rally_id"],
            "supporter_agent_id": r["supporter_agent_id"],
            "supporter_name": r["supporter_name"],
            "stake_shl": micro_to_shl(r["stake_amount"]),
            "message": r.get("message"),
            "status": r["status"],
            "created_at": r.get("created_at", ""),
        })

    return {
        "subtask_id": subtask_id,
        "bounty_shl": micro_to_shl(task["bounty_amount"]),
        "base_bounty_shl": micro_to_shl(task["base_bounty_amount"] or task["bounty_amount"]),
        "escalation_level": task["escalation_level"],
        "status": task["status"],
        "rally_count": len(rallies),
        "total_staked_shl": sum(r["stake_shl"] for r in rallies),
        "rallies": rallies,
    }
