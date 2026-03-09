"""Matchmaker service — tag-based task recommendations."""

import json

import aiosqlite


async def recommend_tasks(db: aiosqlite.Connection, agent_id: str,
                          page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """Recommend open tasks for an agent based on skill_tags overlap.

    Scoring: tag overlap count + difficulty match bonus + reputation proximity bonus.
    """
    # Get agent info
    cur = await db.execute(
        "SELECT skill_tags, reputation_score FROM agents WHERE agent_id = ?",
        (agent_id,),
    )
    agent = await cur.fetchone()
    if not agent:
        return [], 0

    agent_tags = agent["skill_tags"]
    if isinstance(agent_tags, str):
        agent_tags = json.loads(agent_tags)
    agent_tags_set = set(agent_tags)
    agent_rep = agent["reputation_score"]

    # Get all open/claimed tasks not posted by this agent
    cur = await db.execute(
        """SELECT * FROM tasks
           WHERE status IN ('open', 'claimed')
           AND poster_agent_id != ?
           ORDER BY created_at DESC""",
        (agent_id,),
    )
    tasks = [dict(r) for r in await cur.fetchall()]

    # Exclude tasks already claimed by this agent
    cur = await db.execute(
        "SELECT task_id FROM task_claims WHERE solver_agent_id = ? AND status IN ('active', 'submitted')",
        (agent_id,),
    )
    claimed_ids = {r["task_id"] for r in await cur.fetchall()}

    # Score and rank
    scored = []
    for task in tasks:
        if task["task_id"] in claimed_ids:
            continue

        task_tags = task.get("tags", "[]")
        if isinstance(task_tags, str):
            task_tags = json.loads(task_tags)
        task_tags_set = set(task_tags)

        # Tag overlap score (primary)
        overlap = len(agent_tags_set & task_tags_set)

        # Difficulty match bonus
        diff_bonus = 0
        difficulty = task.get("difficulty", "medium")
        if agent_rep < 20 and difficulty == "easy":
            diff_bonus = 1
        elif 20 <= agent_rep < 60 and difficulty == "medium":
            diff_bonus = 1
        elif agent_rep >= 60 and difficulty in ("hard", "expert"):
            diff_bonus = 1

        score = overlap * 3 + diff_bonus
        scored.append((score, task))

    # Sort by score descending, then by created_at descending
    scored.sort(key=lambda x: x[0], reverse=True)

    total = len(scored)
    offset = (page - 1) * page_size
    page_items = scored[offset:offset + page_size]

    return [t for _, t in page_items], total
