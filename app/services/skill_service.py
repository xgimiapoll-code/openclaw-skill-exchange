"""Skill service — skill management and validation."""

import json
import uuid

import aiosqlite


async def create_skill(db: aiosqlite.Connection, author_agent_id: str,
                       name: str, title: str, version: str = "1.0.0",
                       description: str | None = None, category: str = "general",
                       tags: list[str] | None = None, recipe: dict | None = None,
                       is_public: bool = True, source_task_id: str | None = None,
                       fork_of: str | None = None) -> dict:
    """Create a new skill."""
    skill_id = str(uuid.uuid4())

    await db.execute(
        """INSERT INTO skills (skill_id, author_agent_id, name, version, title, description,
           category, tags, recipe, source_task_id, is_public, fork_of)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (skill_id, author_agent_id, name, version, title, description,
         category, json.dumps(tags or []), json.dumps(recipe or {}),
         source_task_id, 1 if is_public else 0, fork_of),
    )
    await db.commit()

    cur = await db.execute("SELECT * FROM skills WHERE skill_id = ?", (skill_id,))
    return dict(await cur.fetchone())


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
    """Install a skill for an agent."""
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
        await db.commit()
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
    await db.commit()

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
    """Fork a skill."""
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
