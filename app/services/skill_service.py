"""Skill service — skill management, validation, and ratings."""

import json
import logging
import uuid

import aiosqlite

from app.config import config
from app.services import wallet_service

logger = logging.getLogger(__name__)


async def create_skill(db: aiosqlite.Connection, author_agent_id: str,
                       name: str, title: str, version: str = "1.0.0",
                       description: str | None = None, category: str = "general",
                       tags: list[str] | None = None, recipe: dict | None = None,
                       is_public: bool = True, source_task_id: str | None = None,
                       fork_of: str | None = None) -> dict:
    """Create a new skill. Caller is responsible for db.commit()."""
    # Validate recipe structure if provided
    if recipe:
        validate_recipe(recipe)

    skill_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO skills (skill_id, author_agent_id, name, version, title, description,
           category, tags, recipe, source_task_id, is_public, fork_of)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (skill_id, author_agent_id, name, version, title, description,
         category, json.dumps(tags or []), json.dumps(recipe or {}),
         source_task_id, 1 if is_public else 0, fork_of),
    )

    cur = await db.execute("SELECT * FROM skills WHERE skill_id = ?", (skill_id,))
    return dict(await cur.fetchone())


def validate_recipe(recipe: dict):
    """Validate recipe JSON has required structure. Raises ValueError if invalid."""
    if not isinstance(recipe, dict):
        raise ValueError("Recipe must be a JSON object")

    # Empty recipe is OK (from submissions without recipe)
    if not recipe:
        return

    # If metadata is provided, validate it
    meta = recipe.get("metadata")
    if meta:
        if not isinstance(meta, dict):
            raise ValueError("Recipe metadata must be a JSON object")
        if "name" in meta and not meta["name"]:
            raise ValueError("Recipe metadata.name cannot be empty")

    # If steps are provided, validate each step
    steps = recipe.get("steps")
    if steps:
        if not isinstance(steps, list):
            raise ValueError("Recipe steps must be a list")
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ValueError(f"Step {i} must be a JSON object")


async def get_skill(db: aiosqlite.Connection, skill_id: str) -> dict | None:
    """Get skill by ID."""
    cur = await db.execute("SELECT * FROM skills WHERE skill_id = ?", (skill_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def list_skills(db: aiosqlite.Connection, category: str | None = None,
                      author_id: str | None = None, search: str | None = None,
                      page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """List public skills with optional filters."""
    conditions = ["is_public = 1"]
    params: list = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if author_id:
        conditions.append("author_agent_id = ?")
        params.append(author_id)
    if search:
        conditions.append("(title LIKE ? OR description LIKE ? OR name LIKE ?)")
        params.extend([f"%{search}%"] * 3)

    where = " WHERE " + " AND ".join(conditions)

    cur = await db.execute(f"SELECT COUNT(*) as cnt FROM skills{where}", params)
    total = (await cur.fetchone())["cnt"]

    offset = (page - 1) * page_size
    cur = await db.execute(
        f"SELECT * FROM skills{where} ORDER BY usage_count DESC, created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows], total


async def install_skill(db: aiosqlite.Connection, agent_id: str, skill_id: str) -> dict:
    """Install a skill for an agent. Checks for skill publish reward threshold.
    Caller is responsible for db.commit().
    """
    skill = await get_skill(db, skill_id)
    if not skill:
        raise ValueError("Skill not found")

    # Check existing install
    cur = await db.execute(
        "SELECT * FROM skill_installs WHERE agent_id = ? AND skill_id = ?",
        (agent_id, skill_id),
    )
    existing = await cur.fetchone()
    if existing:
        # Update version
        await db.execute(
            "UPDATE skill_installs SET installed_version = ? WHERE install_id = ?",
            (skill["version"], existing["install_id"]),
        )
        cur = await db.execute(
            "SELECT * FROM skill_installs WHERE install_id = ?", (existing["install_id"],)
        )
        return dict(await cur.fetchone())

    install_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO skill_installs (install_id, agent_id, skill_id, installed_version) VALUES (?, ?, ?, ?)",
        (install_id, agent_id, skill_id, skill["version"]),
    )

    # Increment usage_count
    await db.execute(
        "UPDATE skills SET usage_count = usage_count + 1, updated_at = datetime('now') WHERE skill_id = ?",
        (skill_id,),
    )

    # Check if skill just reached the publish reward threshold
    new_count = skill["usage_count"] + 1
    if (new_count >= config.skill_publish_min_installs
            and not skill.get("reward_granted")):
        try:
            await wallet_service.grant_skill_reward(
                db, skill["author_agent_id"], skill_id, config.skill_publish_reward_shl
            )
            await db.execute(
                "UPDATE skills SET reward_granted = 1 WHERE skill_id = ?",
                (skill_id,),
            )
        except Exception as e:
            logger.warning("Best-effort skill publish reward failed for skill %s: %s", skill_id, e)

    cur = await db.execute("SELECT * FROM skill_installs WHERE install_id = ?", (install_id,))
    return dict(await cur.fetchone())


async def get_installed_skills(db: aiosqlite.Connection, agent_id: str) -> list[dict]:
    """Get all installed skills for an agent."""
    cur = await db.execute(
        """SELECT si.*, s.name as skill_name, s.title as skill_title
           FROM skill_installs si JOIN skills s ON si.skill_id = s.skill_id
           WHERE si.agent_id = ? ORDER BY si.created_at DESC""",
        (agent_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def fork_skill(db: aiosqlite.Connection, agent_id: str, skill_id: str) -> dict:
    """Fork a skill. Caller is responsible for db.commit()."""
    original = await get_skill(db, skill_id)
    if not original:
        raise ValueError("Skill not found")

    return await create_skill(
        db, agent_id,
        name=original["name"],
        title=f"Fork of {original['title']}",
        description=original.get("description"),
        category=original.get("category", "general"),
        tags=json.loads(original.get("tags", "[]")) if isinstance(original.get("tags"), str) else original.get("tags", []),
        recipe=json.loads(original.get("recipe", "{}")) if isinstance(original.get("recipe"), str) else original.get("recipe", {}),
        fork_of=skill_id,
    )


async def rate_skill(db: aiosqlite.Connection, agent_id: str, skill_id: str,
                     score: int, comment: str | None = None) -> dict:
    """Rate a skill. Updates avg_rating. Caller is responsible for db.commit()."""
    skill = await get_skill(db, skill_id)
    if not skill:
        raise ValueError("Skill not found")

    if skill["author_agent_id"] == agent_id:
        raise ValueError("Cannot rate your own skill")

    # Check existing rating
    cur = await db.execute(
        "SELECT * FROM skill_ratings WHERE skill_id = ? AND agent_id = ?",
        (skill_id, agent_id),
    )
    existing = await cur.fetchone()

    if existing:
        # Update existing rating
        await db.execute(
            "UPDATE skill_ratings SET score = ?, comment = ? WHERE rating_id = ?",
            (score, comment, existing["rating_id"]),
        )
        rating_id = existing["rating_id"]
    else:
        rating_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO skill_ratings (rating_id, skill_id, agent_id, score, comment) VALUES (?, ?, ?, ?, ?)",
            (rating_id, skill_id, agent_id, score, comment),
        )

    # Recalculate avg_rating
    cur = await db.execute(
        "SELECT AVG(score) as avg, COUNT(*) as cnt FROM skill_ratings WHERE skill_id = ?",
        (skill_id,),
    )
    row = await cur.fetchone()
    avg = row["avg"] or 0.0
    await db.execute(
        "UPDATE skills SET avg_rating = ?, updated_at = datetime('now') WHERE skill_id = ?",
        (round(avg, 2), skill_id),
    )

    return {"rating_id": rating_id, "score": score, "skill_avg_rating": round(avg, 2)}
