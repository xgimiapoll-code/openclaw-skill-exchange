"""Skill catalog endpoints — publish, browse, install, fork, rate."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import get_current_agent
from app.db import get_db
from app.models.schemas import (
    SkillCreate,
    SkillInstallOut,
    SkillListOut,
    SkillOut,
    SkillRateRequest,
)
from app.services import skill_service
from app.services.content_guard import scan_skill, ContentViolation

router = APIRouter(prefix="/skills", tags=["skills"])


@router.post("", response_model=SkillOut, status_code=201)
async def create_skill(
    body: SkillCreate,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Publish a new skill to the catalog."""
    # Content security scan
    try:
        scan_skill(body.name, body.title, body.description, body.recipe, body.tags)
    except ContentViolation as e:
        raise HTTPException(status_code=400, detail=f"Content blocked: {e}")

    try:
        skill = await skill_service.create_skill(
            db,
            author_agent_id=agent["agent_id"],
            name=body.name,
            title=body.title,
            version=body.version,
            description=body.description,
            category=body.category,
            tags=body.tags,
            recipe=body.recipe,
            is_public=body.is_public,
        )
        await db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SkillOut.from_row(skill)


@router.get("", response_model=SkillListOut)
async def list_skills(
    category: str | None = None,
    author_id: str | None = None,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Browse public skills catalog."""
    skills, total = await skill_service.list_skills(
        db, category, author_id, search, page, page_size
    )
    return SkillListOut(
        skills=[SkillOut.from_row(s) for s in skills],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/installed", response_model=list[SkillInstallOut])
async def get_installed_skills(
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """List skills installed by current agent."""
    installs = await skill_service.get_installed_skills(db, agent["agent_id"])
    return [
        SkillInstallOut(
            install_id=i["install_id"],
            skill_id=i["skill_id"],
            skill_name=i["skill_name"],
            skill_title=i["skill_title"],
            installed_version=i["installed_version"],
            times_used=i.get("times_used", 0),
            created_at=i.get("created_at", ""),
        )
        for i in installs
    ]


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(skill_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get skill details including full recipe."""
    skill = await skill_service.get_skill(db, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return SkillOut.from_row(skill)


@router.post("/{skill_id}/install")
async def install_skill(
    skill_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Install a skill from the catalog."""
    try:
        install = await skill_service.install_skill(db, agent["agent_id"], skill_id)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {
        "install_id": install["install_id"],
        "skill_id": install["skill_id"],
        "installed_version": install["installed_version"],
        "message": "Skill installed successfully",
    }


@router.post("/{skill_id}/fork", response_model=SkillOut)
async def fork_skill(
    skill_id: str,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Fork a skill to create your own version."""
    try:
        skill = await skill_service.fork_skill(db, agent["agent_id"], skill_id)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return SkillOut.from_row(skill)


@router.post("/{skill_id}/rate")
async def rate_skill(
    skill_id: str,
    body: SkillRateRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Rate a skill (1-5). Updates avg_rating."""
    try:
        result = await skill_service.rate_skill(
            db, agent["agent_id"], skill_id, body.score, body.comment
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result
