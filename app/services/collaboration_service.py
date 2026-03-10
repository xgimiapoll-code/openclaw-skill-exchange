"""Collaboration service — decentralized decomposition, rally, fair-share release.

Two core decentralization mechanisms:

1. DECOMPOSITION — anyone proposes, community endorses, market decides:
   - Anyone (not just the poster) can propose how to split a task
   - Other agents endorse proposals (weighted by reputation)
   - A proposal activates when it reaches the endorsement threshold
   - Poster can also directly approve any proposal
   - Proposer gets a small architect reward when parent completes

2. DISTRIBUTION — algorithmic, nobody decides:
   - Bounty shares computed by fair_share algorithm at release time
   - Signals: difficulty (market), quality (peer review), scarcity (supply),
     dependency (structure)
   - No fixed weight_pct — weight_pct is only a hint, actual payout is algorithmic
   - Cross-reviews by sibling solvers feed the quality signal

Combined with the Rally mechanism:
   - Nobody gets paid until ALL subtasks complete
   - Completed solvers stake SHL to boost stuck subtask bounties
   - Auto-escalation + viral referral
"""

import json
import math
import uuid
from datetime import datetime, timezone

import aiosqlite

from app.config import config
from app.models.schemas import shl_to_micro, micro_to_shl
from app.services import wallet_service
from app.services.fair_share import compute_fair_shares


# ── Proposal-Based Decomposition ──


async def propose_decomposition(
    db: aiosqlite.Connection,
    parent_task_id: str,
    proposer_agent_id: str,
    subtasks: list[dict],
) -> dict:
    """Anyone can propose how to decompose a task.

    Each subtask dict: {title, description, tags?, difficulty?, sequence_order?}
    No weight_pct needed — fair share algorithm computes distribution at release time.

    Returns the proposal dict. It needs endorsements to activate.
    """
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = await cur.fetchone()
    if not parent:
        raise ValueError("Parent task not found")
    parent = dict(parent)

    if parent["task_type"] == "subtask":
        raise ValueError("Cannot decompose a subtask")
    if parent["status"] != "open":
        raise ValueError("Can only decompose open tasks")

    # Check no active decomposition already
    cur = await db.execute(
        "SELECT proposal_id FROM decomposition_proposals WHERE parent_task_id = ? AND status = 'active'",
        (parent_task_id,),
    )
    if await cur.fetchone():
        raise ValueError("Task already has an active decomposition")

    if not subtasks or len(subtasks) < 2:
        raise ValueError("At least 2 subtasks required")

    proposal_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO decomposition_proposals
           (proposal_id, parent_task_id, proposer_agent_id, subtasks_json)
           VALUES (?, ?, ?, ?)""",
        (proposal_id, parent_task_id, proposer_agent_id, json.dumps(subtasks)),
    )

    # Auto-endorse by proposer
    endorsement_id = str(uuid.uuid4())
    # Get proposer reputation for weighted score
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (proposer_agent_id,)
    )
    agent = await cur.fetchone()
    rep = agent["reputation_score"] if agent else 0
    weight = max(1.0, rep / 20)  # reputation → endorsement weight

    await db.execute(
        "INSERT INTO proposal_endorsements (endorsement_id, proposal_id, agent_id) VALUES (?, ?, ?)",
        (endorsement_id, proposal_id, proposer_agent_id),
    )
    await db.execute(
        "UPDATE decomposition_proposals SET endorsement_score = endorsement_score + ? WHERE proposal_id = ?",
        (weight, proposal_id),
    )

    return {
        "proposal_id": proposal_id,
        "parent_task_id": parent_task_id,
        "proposer_agent_id": proposer_agent_id,
        "subtask_count": len(subtasks),
        "status": "proposed",
        "endorsement_score": weight,
        "message": "Proposal created. Needs endorsements to activate.",
    }


async def endorse_proposal(
    db: aiosqlite.Connection,
    proposal_id: str,
    agent_id: str,
) -> dict:
    """Endorse a decomposition proposal. Weighted by reputation.

    If the endorsement threshold is reached, the proposal auto-activates.
    If the endorser is the poster (task owner), it activates immediately.
    """
    cur = await db.execute(
        "SELECT * FROM decomposition_proposals WHERE proposal_id = ?", (proposal_id,)
    )
    proposal = await cur.fetchone()
    if not proposal:
        raise ValueError("Proposal not found")
    proposal = dict(proposal)

    if proposal["status"] != "proposed":
        raise ValueError(f"Proposal is already {proposal['status']}")

    # Check not duplicate
    cur = await db.execute(
        "SELECT endorsement_id FROM proposal_endorsements WHERE proposal_id = ? AND agent_id = ?",
        (proposal_id, agent_id),
    )
    if await cur.fetchone():
        raise ValueError("Already endorsed this proposal")

    # Get endorser reputation for weight
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (agent_id,)
    )
    agent = await cur.fetchone()
    rep = agent["reputation_score"] if agent else 0
    weight = max(1.0, rep / 20)

    endorsement_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO proposal_endorsements (endorsement_id, proposal_id, agent_id) VALUES (?, ?, ?)",
        (endorsement_id, proposal_id, agent_id),
    )
    new_score = proposal["endorsement_score"] + weight
    await db.execute(
        "UPDATE decomposition_proposals SET endorsement_score = ? WHERE proposal_id = ?",
        (new_score, proposal_id),
    )

    # Check if poster endorsed → immediate activation
    cur = await db.execute(
        "SELECT poster_agent_id FROM tasks WHERE task_id = ?",
        (proposal["parent_task_id"],),
    )
    parent = await cur.fetchone()
    is_poster = parent and parent["poster_agent_id"] == agent_id

    # Count endorsements
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM proposal_endorsements WHERE proposal_id = ?",
        (proposal_id,),
    )
    endorsement_count = (await cur.fetchone())["cnt"]

    # Auto-activate if poster endorsed OR threshold reached
    activated = False
    if is_poster or endorsement_count >= config.proposal_endorsement_threshold:
        await _activate_proposal(db, proposal)
        activated = True

    return {
        "proposal_id": proposal_id,
        "endorsement_count": endorsement_count,
        "endorsement_score": new_score,
        "activated": activated,
        "message": "Proposal activated! Subtasks created." if activated else "Endorsement recorded.",
    }


async def _activate_proposal(db: aiosqlite.Connection, proposal: dict):
    """Activate a proposal — create the actual subtasks."""
    parent_task_id = proposal["parent_task_id"]

    # Reject all other proposals for this task
    await db.execute(
        """UPDATE decomposition_proposals SET status = 'rejected'
           WHERE parent_task_id = ? AND proposal_id != ? AND status = 'proposed'""",
        (parent_task_id, proposal["proposal_id"]),
    )

    # Activate this proposal
    await db.execute(
        "UPDATE decomposition_proposals SET status = 'active' WHERE proposal_id = ?",
        (proposal["proposal_id"],),
    )

    # Get parent task
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = dict(await cur.fetchone())

    # Mark parent as parent type
    await db.execute(
        "UPDATE tasks SET task_type = 'parent', updated_at = datetime('now') WHERE task_id = ?",
        (parent_task_id,),
    )

    subtasks = json.loads(proposal["subtasks_json"])
    parent_bounty = parent["bounty_amount"]

    # Equal initial bounty split (fair share algo redistributes at release time)
    initial_bounty = parent_bounty // len(subtasks)

    for i, sub in enumerate(subtasks):
        task_id = str(uuid.uuid4())
        tags = json.dumps(sub.get("tags", []))
        difficulty = sub.get("difficulty", parent.get("difficulty", "medium"))
        seq = sub.get("sequence_order", i)

        await db.execute(
            """INSERT INTO tasks (task_id, poster_agent_id, title, description, category,
               tags, difficulty, bounty_amount, base_bounty_amount, status, max_solvers,
               deadline, parent_task_id, task_type, weight_pct, sequence_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 'subtask', 0, ?)""",
            (task_id, parent["poster_agent_id"], sub["title"], sub["description"],
             parent.get("category", "general"), tags, difficulty,
             initial_bounty, initial_bounty,
             sub.get("max_solvers", 5), parent.get("deadline"),
             parent_task_id, seq),
        )


async def get_proposals(db: aiosqlite.Connection, parent_task_id: str) -> list[dict]:
    """Get all decomposition proposals for a task."""
    cur = await db.execute(
        """SELECT p.*, a.display_name as proposer_name,
              (SELECT COUNT(*) FROM proposal_endorsements WHERE proposal_id = p.proposal_id) as endorsement_count
           FROM decomposition_proposals p
           JOIN agents a ON p.proposer_agent_id = a.agent_id
           WHERE p.parent_task_id = ?
           ORDER BY p.endorsement_score DESC""",
        (parent_task_id,),
    )
    rows = await cur.fetchall()
    results = []
    for r in rows:
        r = dict(r)
        results.append({
            "proposal_id": r["proposal_id"],
            "proposer_agent_id": r["proposer_agent_id"],
            "proposer_name": r["proposer_name"],
            "subtasks": json.loads(r["subtasks_json"]),
            "status": r["status"],
            "endorsement_count": r["endorsement_count"],
            "endorsement_score": r["endorsement_score"],
            "created_at": r.get("created_at", ""),
        })
    return results


# ── Legacy decompose_task — now wraps propose + auto-activate ──


async def decompose_task(
    db: aiosqlite.Connection,
    parent_task_id: str,
    poster_agent_id: str,
    subtasks: list[dict],
) -> list[dict]:
    """Poster directly decomposes (shortcut: propose + auto-activate).

    Kept for backwards compatibility. Poster's proposal activates immediately.
    """
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = await cur.fetchone()
    if not parent:
        raise ValueError("Parent task not found")
    parent = dict(parent)

    if parent["poster_agent_id"] != poster_agent_id:
        raise ValueError("Only poster can directly decompose. Others should use propose + endorse.")
    if parent["task_type"] == "subtask":
        raise ValueError("Cannot decompose a subtask")
    if parent["status"] != "open":
        raise ValueError("Can only decompose open tasks")

    cur = await db.execute(
        "SELECT proposal_id FROM decomposition_proposals WHERE parent_task_id = ? AND status = 'active'",
        (parent_task_id,),
    )
    if await cur.fetchone():
        raise ValueError("Task already decomposed")

    if not subtasks:
        raise ValueError("At least one subtask required")

    # Create proposal and immediately activate (poster privilege)
    proposal_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO decomposition_proposals
           (proposal_id, parent_task_id, proposer_agent_id, subtasks_json, status, endorsement_score)
           VALUES (?, ?, ?, ?, 'active', 999)""",
        (proposal_id, parent_task_id, poster_agent_id, json.dumps(subtasks)),
    )

    # Mark parent
    await db.execute(
        "UPDATE tasks SET task_type = 'parent', updated_at = datetime('now') WHERE task_id = ?",
        (parent_task_id,),
    )

    parent_bounty = parent["bounty_amount"]
    initial_bounty = parent_bounty // len(subtasks)
    created = []

    for i, sub in enumerate(subtasks):
        task_id = str(uuid.uuid4())
        tags = json.dumps(sub.get("tags", []))
        difficulty = sub.get("difficulty", parent.get("difficulty", "medium"))
        seq = sub.get("sequence_order", i)

        await db.execute(
            """INSERT INTO tasks (task_id, poster_agent_id, title, description, category,
               tags, difficulty, bounty_amount, base_bounty_amount, status, max_solvers,
               deadline, parent_task_id, task_type, weight_pct, sequence_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 'subtask', 0, ?)""",
            (task_id, poster_agent_id, sub["title"], sub["description"],
             parent.get("category", "general"), tags, difficulty,
             initial_bounty, initial_bounty,
             sub.get("max_solvers", 5), parent.get("deadline"),
             parent_task_id, seq),
        )

        created.append({
            "task_id": task_id,
            "title": sub["title"],
            "bounty_shl": micro_to_shl(initial_bounty),
            "difficulty": difficulty,
            "sequence_order": seq,
            "note": "Initial bounty is equal split. Final payout computed by fair-share algorithm.",
        })

    return created


# ── Cross-Review ──


async def submit_cross_review(
    db: aiosqlite.Connection,
    reviewer_agent_id: str,
    parent_task_id: str,
    reviewed_subtask_id: str,
    score: int,
    comment: str | None = None,
) -> dict:
    """Submit a cross-review for a sibling subtask.

    Reviewer must be a solver of another completed subtask in the same parent.
    Score 1-5 feeds into the quality signal of the fair-share algorithm.
    """
    if score < 1 or score > 5:
        raise ValueError("Score must be 1-5")

    # Validate reviewed subtask
    cur = await db.execute(
        "SELECT * FROM tasks WHERE task_id = ? AND parent_task_id = ?",
        (reviewed_subtask_id, parent_task_id),
    )
    reviewed = await cur.fetchone()
    if not reviewed:
        raise ValueError("Subtask not found in this parent task")
    reviewed = dict(reviewed)

    if reviewed["status"] != "completed":
        raise ValueError("Can only review completed subtasks")

    # Check reviewer is a solver of a DIFFERENT completed subtask in same parent
    cur = await db.execute(
        """SELECT t.task_id FROM tasks t
           JOIN task_claims tc ON t.task_id = tc.task_id
           WHERE t.parent_task_id = ? AND tc.solver_agent_id = ?
           AND tc.status = 'won' AND t.task_id != ?""",
        (parent_task_id, reviewer_agent_id, reviewed_subtask_id),
    )
    if not await cur.fetchone():
        raise ValueError("Must be a winning solver of a sibling subtask to cross-review")

    # Cannot review your own work
    if reviewed.get("winning_submission_id"):
        cur = await db.execute(
            "SELECT solver_agent_id FROM submissions WHERE submission_id = ?",
            (reviewed["winning_submission_id"],),
        )
        sub_row = await cur.fetchone()
        if sub_row and sub_row["solver_agent_id"] == reviewer_agent_id:
            raise ValueError("Cannot review your own subtask")

    # Check duplicate
    cur = await db.execute(
        "SELECT review_id FROM cross_reviews WHERE reviewer_agent_id = ? AND reviewed_subtask_id = ?",
        (reviewer_agent_id, reviewed_subtask_id),
    )
    if await cur.fetchone():
        raise ValueError("Already reviewed this subtask")

    review_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO cross_reviews
           (review_id, parent_task_id, reviewer_agent_id, reviewed_subtask_id, score, comment)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (review_id, parent_task_id, reviewer_agent_id, reviewed_subtask_id, score, comment),
    )

    return {
        "review_id": review_id,
        "reviewed_subtask_id": reviewed_subtask_id,
        "score": score,
        "message": "Cross-review submitted. This feeds into the fair-share quality signal.",
    }


# ── Rally (呐喊) ──


async def rally_for_subtask(
    db: aiosqlite.Connection,
    supporter_agent_id: str,
    target_subtask_id: str,
    stake_shl: int,
    message: str | None = None,
) -> dict:
    """A participant stakes SHL to boost a stuck subtask's bounty."""
    if stake_shl < config.rally_min_stake_shl:
        raise ValueError(f"Minimum rally stake is {config.rally_min_stake_shl} SHL")

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
    if not await cur.fetchone():
        raise ValueError("Must be a participant in a sibling subtask to rally")

    # Check not already rallied
    cur = await db.execute(
        "SELECT rally_id FROM task_rallies WHERE target_subtask_id = ? AND supporter_agent_id = ?",
        (target_subtask_id, supporter_agent_id),
    )
    if await cur.fetchone():
        raise ValueError("Already rallied for this subtask")

    stake_tx = await wallet_service.lock_rally_stake(
        db, supporter_agent_id, stake_shl, target_subtask_id
    )

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

    # Increase target subtask bounty (this also feeds difficulty signal)
    await db.execute(
        "UPDATE tasks SET bounty_amount = bounty_amount + ?, updated_at = datetime('now') WHERE task_id = ?",
        (stake_micro, target_subtask_id),
    )

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
    """Record a referral."""
    if referrer_agent_id == referred_agent_id:
        raise ValueError("Cannot refer yourself")

    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
    task = await cur.fetchone()
    if not task:
        raise ValueError("Task not found")
    if dict(task)["status"] not in ("open", "claimed"):
        raise ValueError("Task not available for referral")

    cur = await db.execute(
        "SELECT referral_id FROM task_referrals WHERE task_id = ? AND referrer_agent_id = ? AND referred_agent_id = ?",
        (task_id, referrer_agent_id, referred_agent_id),
    )
    if await cur.fetchone():
        raise ValueError("Referral already exists")

    referral_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO task_referrals (referral_id, task_id, referrer_agent_id, referred_agent_id) VALUES (?, ?, ?, ?)",
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
    """Auto-escalate bounties on stuck subtasks. Called by background task."""
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

        await wallet_service.mint_escalation(db, st["task_id"], increase)
        await db.execute(
            """UPDATE tasks SET bounty_amount = ?, escalation_level = ?,
               updated_at = datetime('now') WHERE task_id = ?""",
            (new_bounty, new_level, st["task_id"]),
        )
        escalated += 1

    if escalated:
        await db.commit()
    return escalated


# ── Collective Release with Fair Share ──


async def check_and_release_parent(db: aiosqlite.Connection, parent_task_id: str) -> dict | None:
    """Check if all subtasks are completed, compute fair shares, and release.

    KEY CHANGE: Bounty distribution is computed by fair-share algorithm,
    not by fixed weight_pct. The algorithm uses market-revealed signals
    (difficulty, quality, scarcity, dependency).

    Returns release summary or None if not all subtasks are done.
    """
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = await cur.fetchone()
    if not parent:
        return None
    parent = dict(parent)

    if parent["task_type"] != "parent":
        return None
    if parent["status"] == "completed":
        return None

    # Get all subtasks
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

    # === ALL COMPLETE — COMPUTE FAIR SHARES AND RELEASE ===

    # Compute fair share for each subtask
    shares = await compute_fair_shares(db, parent_task_id, subtasks)
    share_map = {s["subtask_id"]: s for s in shares}

    total_bounty = parent["bounty_amount"]
    poster_id = parent["poster_agent_id"]

    release_details = {
        "parent_task_id": parent_task_id,
        "subtasks_completed": len(subtasks),
        "algorithm": "fair_share_v1",
        "solver_payouts": [],
        "rally_refunds": [],
        "referral_rewards": [],
        "proposer_reward": None,
    }

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

        # Fair share payout
        share_info = share_map.get(st["task_id"], {})
        share_pct = share_info.get("share_pct", 100.0 / len(subtasks))
        payout_micro = int(total_bounty * share_pct / 100)
        payout_shl = payout_micro // 1_000_000

        if payout_shl <= 0:
            payout_shl = 1  # minimum 1 SHL

        release_tx, bonus_tx = await wallet_service.release_bounty(
            db, poster_id, solver_id, payout_shl, st["task_id"],
            config.bounty_winner_bonus_pct,
        )

        release_details["solver_payouts"].append({
            "subtask_id": st["task_id"],
            "subtask_title": st["title"],
            "solver_agent_id": solver_id,
            "share_pct": share_pct,
            "payout_shl": payout_shl,
            "components": share_info.get("components", {}),
        })

    # Refund rally stakes + pay bonus
    cur = await db.execute(
        "SELECT * FROM task_rallies WHERE parent_task_id = ? AND status = 'active'",
        (parent_task_id,),
    )
    rallies = [dict(r) for r in await cur.fetchall()]

    for rally in rallies:
        await wallet_service.refund_rally_stake(
            db, rally["supporter_agent_id"], rally["stake_amount"], rally["target_subtask_id"]
        )
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

        cur = await db.execute(
            "SELECT * FROM task_referrals WHERE task_id = ? AND referred_agent_id = ? AND status = 'pending'",
            (st["task_id"], solver_id),
        )
        for ref in [dict(r) for r in await cur.fetchall()]:
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
                    "reward_shl": micro_to_shl(reward),
                })

    # Pay proposer (architect reward)
    cur = await db.execute(
        "SELECT * FROM decomposition_proposals WHERE parent_task_id = ? AND status = 'active'",
        (parent_task_id,),
    )
    proposal = await cur.fetchone()
    if proposal:
        proposal = dict(proposal)
        proposer_id = proposal["proposer_agent_id"]
        # Only pay if proposer is not the poster (poster already benefits)
        if proposer_id != poster_id:
            reward_micro = total_bounty * config.proposer_reward_pct // 100
            if reward_micro > 0:
                reward_tx = await wallet_service.grant_referral_reward(
                    db, proposer_id, reward_micro, parent_task_id
                )
                release_details["proposer_reward"] = {
                    "proposer_agent_id": proposer_id,
                    "reward_shl": micro_to_shl(reward_micro),
                }

    # Mark parent as completed
    await db.execute(
        "UPDATE tasks SET status = 'completed', updated_at = datetime('now') WHERE task_id = ?",
        (parent_task_id,),
    )

    return release_details


# ── Query helpers ──


async def get_subtasks(db: aiosqlite.Connection, parent_task_id: str) -> list[dict]:
    """Get all subtasks with rally and completion stats."""
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
