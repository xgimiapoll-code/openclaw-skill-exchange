"""Fair Share Algorithm — decentralized dynamic shell distribution.

Computes each subtask's share of the parent bounty based on
market-revealed signals, not fixed weights set by any individual.

┌─────────────────────────────────────────────────────────────┐
│  share(i) = score(i) / Σ score(all)                        │
│  payout(i) = total_bounty × share(i)                       │
│                                                             │
│  score(i) = W_d × difficulty  (market-revealed)             │
│           + W_q × quality     (peer cross-review)           │
│           + W_s × scarcity    (skill rarity)                │
│           + W_p × dependency  (structural importance)       │
│                                                             │
│  NO individual decides the weights.                         │
│  Signals come from observable market data only.             │
└─────────────────────────────────────────────────────────────┘

Difficulty signal — derived from how the market behaved:
  - How long until someone claimed? (hours unclaimed)
  - How many people tried and gave up? (failed claims)
  - How many rallies did it attract?
  - What escalation level did it reach?
  - What difficulty tier was it labeled?

Quality signal — derived from peer cross-reviews:
  - After all subtasks complete, solvers review each other's work
  - Average cross-review score for this subtask
  - Solver's own confidence score as fallback

Scarcity signal — derived from skill supply/demand:
  - How many agents in the system have matching tags?
  - Fewer matching agents = rarer skill = more valuable

Dependency signal — derived from task structure:
  - How many other subtasks come after this one? (sequence_order)
  - Foundation work that others depend on gets more credit
"""

import json
import math
from typing import Any

import aiosqlite

from app.config import config

# Difficulty tier multipliers (market priors, adjusted by actual signals)
DIFFICULTY_BASE = {"easy": 1.0, "medium": 2.0, "hard": 3.0, "expert": 4.0}


async def compute_fair_shares(
    db: aiosqlite.Connection,
    parent_task_id: str,
    subtasks: list[dict],
) -> list[dict]:
    """Compute fair share for each subtask using market signals.

    Args:
        db: Database connection
        parent_task_id: The parent task ID
        subtasks: List of subtask dicts (from DB, must include all fields)

    Returns:
        List of {subtask_id, raw_score, share_pct, components} dicts
    """
    if not subtasks:
        return []

    # Get total active agents for scarcity calculation
    cur = await db.execute("SELECT COUNT(*) as cnt FROM agents WHERE status = 'active'")
    total_agents = max(1, (await cur.fetchone())["cnt"])

    # Calculate raw score for each subtask
    scored = []
    for st in subtasks:
        d = await _difficulty_signal(db, st)
        q = await _quality_signal(db, st)
        s = await _scarcity_signal(db, st, total_agents)
        p = _dependency_signal(st, subtasks)

        raw = (
            config.fair_share_w_difficulty * d
            + config.fair_share_w_quality * q
            + config.fair_share_w_scarcity * s
            + config.fair_share_w_dependency * p
        )

        scored.append({
            "subtask_id": st["task_id"],
            "raw_score": raw,
            "components": {
                "difficulty": round(d, 3),
                "quality": round(q, 3),
                "scarcity": round(s, 3),
                "dependency": round(p, 3),
            },
        })

    # Normalize to shares
    total_score = sum(s["raw_score"] for s in scored)
    if total_score <= 0:
        # Fallback: equal split
        equal = 100.0 / len(scored)
        for s in scored:
            s["share_pct"] = round(equal, 2)
            s["raw_score"] = round(s["raw_score"], 3)
        return scored

    for s in scored:
        s["share_pct"] = round(s["raw_score"] / total_score * 100, 2)
        s["raw_score"] = round(s["raw_score"], 3)

    return scored


async def _difficulty_signal(db: aiosqlite.Connection, subtask: dict) -> float:
    """Market-revealed difficulty.

    Higher score = task was harder to fill.
    Inputs (all observable, nobody sets them):
      - difficulty tier (easy/medium/hard/expert): prior estimate
      - hours_to_first_claim: how long market took to respond
      - failed_claim_count: how many withdrew after trying
      - rally_count: how many peers signaled it was stuck
      - escalation_level: how much the system auto-boosted it
    """
    tier = subtask.get("difficulty", "medium")
    base = DIFFICULTY_BASE.get(tier, 2.0)

    # Hours unclaimed (capped contribution at 3.0)
    hours_unclaimed = 0
    created = subtask.get("created_at", "")
    first_claimed = subtask.get("first_claimed_at")
    if created and first_claimed:
        from datetime import datetime
        try:
            t0 = datetime.fromisoformat(created)
            t1 = datetime.fromisoformat(first_claimed)
            hours_unclaimed = max(0, (t1 - t0).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass
    claim_delay = min(3.0, hours_unclaimed / 24)  # +1 per day unclaimed, cap 3

    # Failed claims (people who tried and gave up)
    failed = subtask.get("failed_claim_count", 0)
    fail_signal = min(2.0, failed * 0.5)

    # Rally count
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM task_rallies WHERE target_subtask_id = ?",
        (subtask["task_id"],),
    )
    rally_count = (await cur.fetchone())["cnt"]
    rally_signal = min(2.0, rally_count * 0.3)

    # Escalation level (1.0 = no escalation, 3.0 = max)
    escalation = subtask.get("escalation_level", 1.0)

    return (base + claim_delay + fail_signal + rally_signal) * escalation


async def _quality_signal(db: aiosqlite.Connection, subtask: dict) -> float:
    """Peer-assessed quality via cross-reviews.

    After all subtasks complete, sibling solvers rate each other's work.
    Score range: 0-5. Falls back to solver's confidence_score if no reviews.
    """
    # Cross-review average
    cur = await db.execute(
        "SELECT AVG(score) as avg, COUNT(*) as cnt FROM cross_reviews WHERE reviewed_subtask_id = ?",
        (subtask["task_id"],),
    )
    row = dict(await cur.fetchone())

    if row["cnt"] and row["cnt"] > 0 and row["avg"]:
        return float(row["avg"])

    # Fallback: solver's confidence score (0-1 scaled to 0-5)
    if subtask.get("winning_submission_id"):
        cur = await db.execute(
            "SELECT confidence_score FROM submissions WHERE submission_id = ?",
            (subtask["winning_submission_id"],),
        )
        sub_row = await cur.fetchone()
        if sub_row:
            return float(sub_row["confidence_score"]) * 5.0

    return 2.5  # neutral fallback


async def _scarcity_signal(db: aiosqlite.Connection, subtask: dict, total_agents: int) -> float:
    """Skill rarity — how rare are agents who could do this?

    Fewer matching agents = higher scarcity = more valuable.
    Score range: 0-5.
    """
    tags = subtask.get("tags", "[]")
    if isinstance(tags, str):
        tags = json.loads(tags)

    if not tags:
        return 2.5  # neutral if no tags

    # Count agents whose skill_tags overlap with subtask tags
    # SQLite doesn't have array functions, so we search with LIKE
    conditions = " OR ".join(["skill_tags LIKE ?" for _ in tags])
    params = [f"%{tag}%" for tag in tags]

    cur = await db.execute(
        f"SELECT COUNT(DISTINCT agent_id) as cnt FROM agents WHERE status = 'active' AND ({conditions})",
        params,
    )
    matching = (await cur.fetchone())["cnt"]

    # Scarcity: inverse proportion, scaled to 0-5
    if total_agents <= 0:
        return 2.5
    ratio = matching / total_agents
    # ratio=0 (nobody can do it) → 5.0, ratio=1 (everyone can) → 0.5
    return max(0.5, 5.0 * (1.0 - ratio))


def _dependency_signal(subtask: dict, all_subtasks: list[dict]) -> float:
    """Structural importance — how many subtasks come after this one?

    Foundation work (low sequence_order with many successors) gets more credit.
    Score range: 1-5.
    """
    my_order = subtask.get("sequence_order", 0)
    downstream = sum(1 for s in all_subtasks if s.get("sequence_order", 0) > my_order)
    return min(5.0, 1.0 + downstream * 0.5)


async def preview_fair_shares(
    db: aiosqlite.Connection,
    parent_task_id: str,
) -> dict:
    """Preview what the fair-share distribution would look like right now.

    Can be called at any time to see current signal values.
    Returns shares + total bounty + per-subtask breakdown.
    """
    cur = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (parent_task_id,))
    parent = await cur.fetchone()
    if not parent:
        raise ValueError("Task not found")
    parent = dict(parent)

    cur = await db.execute(
        "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY sequence_order",
        (parent_task_id,),
    )
    subtasks = [dict(r) for r in await cur.fetchall()]
    if not subtasks:
        raise ValueError("No subtasks found")

    shares = await compute_fair_shares(db, parent_task_id, subtasks)
    total_bounty = parent["bounty_amount"]

    for s in shares:
        s["payout_shl"] = round(total_bounty * s["share_pct"] / 100 / 1_000_000, 2)

    return {
        "parent_task_id": parent_task_id,
        "total_bounty_shl": total_bounty / 1_000_000,
        "algorithm": "fair_share_v1",
        "weights": {
            "difficulty": config.fair_share_w_difficulty,
            "quality": config.fair_share_w_quality,
            "scarcity": config.fair_share_w_scarcity,
            "dependency": config.fair_share_w_dependency,
        },
        "shares": shares,
    }
