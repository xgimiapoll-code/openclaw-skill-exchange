"""Matchmaker service — multi-factor task & skill recommendations.

Scoring factors for tasks:
  - tag_overlap (40%): Jaccard similarity between agent skills and task tags
  - category_affinity (25%): based on agent's past solved task categories
  - difficulty_match (20%): match difficulty to agent reputation tier
  - freshness (15%): newer tasks score higher
"""

import json
from datetime import datetime, timezone

import aiosqlite

from app.services.rate_limiter import get_tier

# Difficulty-tier affinity matrix
_DIFF_TIER = {
    "Newcomer": {"easy": 1.0, "medium": 0.5, "hard": 0.2, "expert": 0.1},
    "Contributor": {"easy": 0.5, "medium": 1.0, "hard": 0.5, "expert": 0.2},
    "Specialist": {"easy": 0.2, "medium": 0.5, "hard": 1.0, "expert": 0.5},
    "Expert": {"easy": 0.1, "medium": 0.3, "hard": 0.7, "expert": 1.0},
    "Master": {"easy": 0.1, "medium": 0.2, "hard": 0.7, "expert": 1.0},
}


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _freshness_score(created_at: str) -> float:
    """1.0 for today, decays by 0.1 per day, min 0.1."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days_old = (datetime.now(timezone.utc) - created).days
        return max(0.1, 1.0 - days_old * 0.1)
    except (ValueError, AttributeError):
        return 0.5


async def recommend_tasks(db: aiosqlite.Connection, agent_id: str,
                          page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """Recommend open tasks using multi-factor scoring."""
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
    tier_name = get_tier(agent_rep)[0]

    # Get category affinity from past completed tasks
    cur = await db.execute(
        """SELECT t.category, COUNT(*) as cnt
           FROM task_claims tc JOIN tasks t ON tc.task_id = t.task_id
           WHERE tc.solver_agent_id = ? AND tc.status = 'won'
           GROUP BY t.category""",
        (agent_id,),
    )
    cat_rows = await cur.fetchall()
    cat_counts = {r["category"]: r["cnt"] for r in cat_rows}
    total_solved = sum(cat_counts.values()) if cat_counts else 0

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

        # Factor 1: Tag overlap (Jaccard)
        tag_score = _jaccard(agent_tags_set, task_tags_set)

        # Factor 2: Category affinity
        category = task.get("category", "general")
        if total_solved > 0:
            cat_affinity = cat_counts.get(category, 0) / total_solved
        else:
            cat_affinity = 0.5 if category == "general" else 0.3

        # Factor 3: Difficulty match
        difficulty = task.get("difficulty", "medium")
        diff_match = _DIFF_TIER.get(tier_name, {}).get(difficulty, 0.5)

        # Factor 4: Freshness
        freshness = _freshness_score(task.get("created_at", ""))

        # Weighted score
        score = (
            0.40 * tag_score +
            0.25 * cat_affinity +
            0.20 * diff_match +
            0.15 * freshness
        )
        scored.append((score, task))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    total = len(scored)
    offset = (page - 1) * page_size
    page_items = scored[offset:offset + page_size]

    return [t for _, t in page_items], total


async def recommend_skills(db: aiosqlite.Connection, agent_id: str,
                           page: int = 1, page_size: int = 10) -> tuple[list[dict], int]:
    """Recommend skills the agent hasn't installed yet.

    Scoring: tag overlap + popularity + rating.
    """
    # Get agent skill tags
    cur = await db.execute(
        "SELECT skill_tags FROM agents WHERE agent_id = ?", (agent_id,)
    )
    agent = await cur.fetchone()
    if not agent:
        return [], 0

    agent_tags = agent["skill_tags"]
    if isinstance(agent_tags, str):
        agent_tags = json.loads(agent_tags)
    agent_tags_set = set(agent_tags)

    # Get already installed skill IDs
    cur = await db.execute(
        "SELECT skill_id FROM skill_installs WHERE agent_id = ?", (agent_id,)
    )
    installed_ids = {r["skill_id"] for r in await cur.fetchall()}

    # Get all public skills
    cur = await db.execute(
        "SELECT * FROM skills WHERE is_public = 1 AND author_agent_id != ? ORDER BY usage_count DESC",
        (agent_id,),
    )
    skills = [dict(r) for r in await cur.fetchall()]

    # Dedupe to latest version per name+author
    seen = set()
    unique_skills = []
    for s in skills:
        key = (s["name"], s["author_agent_id"])
        if key not in seen:
            seen.add(key)
            unique_skills.append(s)

    scored = []
    for skill in unique_skills:
        if skill["skill_id"] in installed_ids:
            continue

        skill_tags = skill.get("tags", "[]")
        if isinstance(skill_tags, str):
            skill_tags = json.loads(skill_tags)
        skill_tags_set = set(skill_tags)

        tag_score = _jaccard(agent_tags_set, skill_tags_set)
        popularity = min(1.0, skill.get("usage_count", 0) / 10)  # normalize to 10
        rating = skill.get("avg_rating", 0) / 5.0

        score = 0.40 * tag_score + 0.35 * popularity + 0.25 * rating
        scored.append((score, skill))

    scored.sort(key=lambda x: x[0], reverse=True)
    total = len(scored)
    offset = (page - 1) * page_size
    page_items = scored[offset:offset + page_size]

    return [s for _, s in page_items], total
