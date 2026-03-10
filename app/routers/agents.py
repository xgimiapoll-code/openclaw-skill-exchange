"""Agent registration and profile endpoints."""

import json
import secrets
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.deps import get_current_agent
from app.db import get_db
from app.models.schemas import (
    AgentOut,
    AgentRegister,
    AgentRegisterOut,
    micro_to_shl,
)
from app.services.content_guard import scan_text, scan_tags, ContentViolation
from app.services.tx_guard import check_registration_rate, TxVelocityViolation
from app.services.wallet_service import create_wallet

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/register", response_model=AgentRegisterOut, status_code=201)
async def register_agent(body: AgentRegister, db: aiosqlite.Connection = Depends(get_db)):
    """Register a new agent. Returns agent profile + API key + initial wallet balance."""
    # Anti-sybil: registration rate limit
    try:
        await check_registration_rate(db, body.node_id)
    except TxVelocityViolation as e:
        raise HTTPException(status_code=429, detail=str(e))

    # Content scan on display name and tags
    try:
        scan_text(body.display_name, "display_name")
        if body.skill_tags:
            scan_tags(body.skill_tags, "skill_tags")
    except ContentViolation as e:
        raise HTTPException(status_code=400, detail=f"Content blocked: {e}")

    # Check duplicate node_id
    cur = await db.execute("SELECT agent_id FROM agents WHERE node_id = ?", (body.node_id,))
    if await cur.fetchone():
        raise HTTPException(status_code=409, detail="node_id already registered")

    agent_id = str(uuid.uuid4())
    api_key = f"sk-{secrets.token_hex(32)}"

    await db.execute(
        """INSERT INTO agents (agent_id, node_id, display_name, public_key, wallet_address, api_key, skill_tags)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, body.node_id, body.display_name, body.public_key,
         body.wallet_address, api_key, json.dumps(body.skill_tags)),
    )

    wallet_id = await create_wallet(db, agent_id)
    await db.commit()

    cur = await db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
    agent_row = dict(await cur.fetchone())

    cur = await db.execute("SELECT balance FROM wallets WHERE agent_id = ?", (agent_id,))
    wallet = await cur.fetchone()

    return AgentRegisterOut(
        agent=AgentOut.from_row(agent_row),
        api_key=api_key,
        wallet_balance_shl=micro_to_shl(wallet["balance"]),
    )


@router.get("/me", response_model=AgentOut)
async def get_me(agent: dict = Depends(get_current_agent)):
    """Get current agent's profile."""
    return AgentOut.from_row(agent)


class AgentUpdate(BaseModel):
    display_name: str | None = None
    skill_tags: list[str] | None = None
    wallet_address: str | None = None


@router.patch("/me", response_model=AgentOut)
async def update_me(
    body: AgentUpdate,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Update current agent's profile."""
    if body.display_name:
        await db.execute(
            "UPDATE agents SET display_name = ?, updated_at = datetime('now') WHERE agent_id = ?",
            (body.display_name, agent["agent_id"]),
        )
    if body.skill_tags is not None:
        await db.execute(
            "UPDATE agents SET skill_tags = ?, updated_at = datetime('now') WHERE agent_id = ?",
            (json.dumps(body.skill_tags), agent["agent_id"]),
        )
    if body.wallet_address is not None:
        await db.execute(
            "UPDATE agents SET wallet_address = ?, updated_at = datetime('now') WHERE agent_id = ?",
            (body.wallet_address, agent["agent_id"]),
        )
    await db.commit()
    cur = await db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent["agent_id"],))
    return AgentOut.from_row(dict(await cur.fetchone()))


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: str, db: aiosqlite.Connection = Depends(get_db)):
    """Get public agent profile."""
    cur = await db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
    row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Agent not found")
    return AgentOut.from_row(dict(row))


@router.post("/me/rotate-key")
async def rotate_api_key(
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Generate a new API key. The old key is immediately invalidated."""
    new_key = f"sk-{secrets.token_hex(32)}"
    await db.execute(
        "UPDATE agents SET api_key = ?, updated_at = datetime('now') WHERE agent_id = ?",
        (new_key, agent["agent_id"]),
    )
    await db.commit()
    return {"api_key": new_key, "message": "API key rotated. Old key is now invalid."}
