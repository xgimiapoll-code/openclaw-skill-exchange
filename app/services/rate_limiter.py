"""Rate limiter — reputation-based daily action limits."""

import aiosqlite


# Tier definitions: (min_score, max_score, post_limit, claim_limit)
# None means unlimited
TIERS = [
    ("Newcomer",    0,  19, 10,   20),
    ("Contributor", 20, 39, 20,   50),
    ("Specialist",  40, 59, 50,   None),
    ("Expert",      60, 79, None, None),
    ("Master",      80, 100, None, None),
]


def get_tier(reputation_score: float) -> tuple[str, int | None, int | None]:
    """Return (tier_name, post_limit, claim_limit) for a given reputation score."""
    for name, min_s, max_s, post_lim, claim_lim in TIERS:
        if min_s <= reputation_score <= max_s:
            return name, post_lim, claim_lim
    # Default to Newcomer for out-of-range scores
    if reputation_score < 0:
        return "Newcomer", 1, 2
    return "Master", None, None


async def check_daily_limit(db: aiosqlite.Connection, agent_id: str, action: str) -> tuple[bool, str]:
    """Check if an agent can perform the given action today.

    action: "post" or "claim"
    Returns (allowed, reason).
    """
    # Get agent reputation
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (agent_id,)
    )
    agent = await cur.fetchone()
    if not agent:
        return False, "Agent not found"

    tier_name, post_limit, claim_limit = get_tier(agent["reputation_score"])

    if action == "post":
        limit = post_limit
    elif action == "claim":
        limit = claim_limit
    else:
        return True, "Unknown action, allowing"

    if limit is None:
        return True, f"{tier_name}: unlimited"

    # Count today's actions
    if action == "post":
        cur = await db.execute(
            """SELECT COUNT(*) as cnt FROM tasks
               WHERE poster_agent_id = ? AND date(created_at) = date('now')""",
            (agent_id,),
        )
    else:  # claim
        cur = await db.execute(
            """SELECT COUNT(*) as cnt FROM task_claims
               WHERE solver_agent_id = ? AND date(created_at) = date('now')""",
            (agent_id,),
        )

    count = (await cur.fetchone())["cnt"]

    if count >= limit:
        return False, f"{tier_name} daily limit reached: {count}/{limit} {action}s today"

    return True, f"{tier_name}: {count}/{limit} {action}s today"
