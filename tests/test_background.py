"""Background task tests.

Covers: weekly rewards, skill publish rewards, auto-resolve disputes,
reputation recalculation, stuck subtask escalation.
"""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "bg_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

os.environ["MARKET_DB_PATH"] = DB_PATH

import importlib
for mod in ["app.config", "app.db"]:
    if mod in sys.modules:
        del sys.modules[mod]

import app.config
importlib.reload(app.config)
import app.db
importlib.reload(app.db)

from app.db import init_db, get_db_ctx  # noqa: E402
from app.main import app  # noqa: E402
from app.config import config  # noqa: E402

state: dict = {}


@pytest_asyncio.fixture(scope="module")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register(client, node_id, name, skill_tags=None):
    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": node_id, "display_name": name, "skill_tags": skill_tags or []},
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["api_key"], data["agent"]["agent_id"]


def auth(key):
    return {"Authorization": f"Bearer {key}"}


# ── Setup ──


async def test_setup(client):
    """Register agents for background task tests."""
    state["alice_key"], state["alice_id"] = await register(
        client, "bg-alice", "Alice", ["python", "fastapi"]
    )
    state["bob_key"], state["bob_id"] = await register(
        client, "bg-bob", "Bob", ["docker", "devops"]
    )
    state["carol_key"], state["carol_id"] = await register(
        client, "bg-carol", "Carol", ["rust", "systems"]
    )


# ── distribute_weekly_rewards ──


async def test_distribute_weekly_rewards(client):
    """Active agents receive weekly activity reward after 7-day cooldown."""
    from app.background.tasks import distribute_weekly_rewards

    # Create activity: Alice posts a task
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Weekly reward test", "description": "Test", "bounty_shl": 5},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201

    # Set last_activity_reward to 8 days ago so cooldown has passed
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE agents SET last_activity_reward = datetime('now', '-8 days') WHERE agent_id = ?",
            (state["alice_id"],),
        )
        await db.commit()

    # Record balance before
    wallet_before = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    balance_before = wallet_before["balance_shl"]

    # Run background task
    await distribute_weekly_rewards()

    # Verify balance increased by weekly_activity_shl
    wallet_after = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    assert wallet_after["balance_shl"] == balance_before + config.weekly_activity_shl


async def test_distribute_weekly_rewards_no_double(client):
    """Weekly reward is not granted again within 7-day cooldown."""
    from app.background.tasks import distribute_weekly_rewards

    wallet_before = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    balance_before = wallet_before["balance_shl"]

    # Run again immediately -- cooldown should prevent re-grant
    await distribute_weekly_rewards()

    wallet_after = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    assert wallet_after["balance_shl"] == balance_before


# ── check_skill_publish_rewards ──


async def test_check_skill_publish_rewards(client):
    """Skill author receives reward when usage_count reaches threshold."""
    from app.background.tasks import check_skill_publish_rewards

    # Alice publishes a skill
    resp = await client.post(
        "/v1/market/skills",
        json={"name": "bg-reward-skill", "title": "BG Reward Skill"},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    skill_id = resp.json()["skill_id"]

    # Directly set usage_count >= threshold and reward_granted = 0
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE skills SET usage_count = ?, reward_granted = 0 WHERE skill_id = ?",
            (config.skill_publish_min_installs, skill_id),
        )
        await db.commit()

    wallet_before = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    balance_before = wallet_before["balance_shl"]

    # Run background task
    await check_skill_publish_rewards()

    # Verify reward granted
    wallet_after = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    assert wallet_after["balance_shl"] == balance_before + config.skill_publish_reward_shl

    # Verify reward_granted flag is set
    async with get_db_ctx() as db:
        cur = await db.execute(
            "SELECT reward_granted FROM skills WHERE skill_id = ?", (skill_id,)
        )
        row = await cur.fetchone()
        assert row["reward_granted"] == 1


async def test_check_skill_publish_rewards_no_double(client):
    """Skill publish reward is not granted twice."""
    from app.background.tasks import check_skill_publish_rewards

    wallet_before = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    balance_before = wallet_before["balance_shl"]

    # Run again -- reward_granted=1 should prevent re-grant
    await check_skill_publish_rewards()

    wallet_after = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    assert wallet_after["balance_shl"] == balance_before


# ── auto_resolve_disputes ──


async def test_auto_resolve_disputes(client):
    """Auto-resolve disputes open for 72+ hours with resolution_method='auto'."""
    from app.background.tasks import auto_resolve_disputes

    # Create a complete task cycle: Alice posts, Bob claims, submits, Alice selects winner
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Dispute auto-resolve test", "description": "Test", "bounty_shl": 5},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/submissions",
        json={"summary": "My solution", "skill_recipe": {}, "confidence_score": 0.8},
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 201
    sub_id = resp.json()["submission_id"]

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/select-winner",
        json={"submission_id": sub_id, "feedback": "OK", "rating": 3},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 200

    # Alice opens a dispute
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/dispute",
        json={"reason": "Solution was incomplete", "evidence": {"notes": "missing tests"}},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    dispute_id = resp.json()["dispute_id"]
    assert resp.json()["resolution_method"] == "auto"

    # Set created_at to 73 hours ago so auto-resolve triggers
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE disputes SET created_at = datetime('now', '-73 hours') WHERE dispute_id = ?",
            (dispute_id,),
        )
        await db.commit()

    # Run auto-resolve
    await auto_resolve_disputes()

    # Verify dispute is resolved
    resp = await client.get(f"/v1/market/disputes/{dispute_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("resolved_initiator", "resolved_respondent")
    assert data["resolved_at"] is not None


# ── recalculate_reputation ──


async def test_recalculate_reputation(client):
    """Reputation recalculation updates agent's reputation_score."""
    from app.background.tasks import recalculate_reputation

    # Get current reputation
    resp = await client.get(
        "/v1/market/reputation/me",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200

    # Recalculate
    new_score = await recalculate_reputation(state["bob_id"])

    # Verify score was stored
    async with get_db_ctx() as db:
        cur = await db.execute(
            "SELECT reputation_score FROM agents WHERE agent_id = ?",
            (state["bob_id"],),
        )
        row = await cur.fetchone()
        assert row["reputation_score"] == round(new_score, 2)


async def test_recalculate_reputation_nonexistent():
    """Reputation recalculation for nonexistent agent does not crash."""
    from app.background.tasks import recalculate_reputation

    # Should return 0.0 gracefully, not raise
    result = await recalculate_reputation("nonexistent-agent-id-000")
    assert result == 0.0


# ── escalate_stuck_subtasks ──


async def test_escalate_stuck_subtasks(client):
    """Stuck subtasks get their bounty auto-escalated."""
    from app.services.collaboration_service import escalate_stuck_subtasks

    # Alice creates a parent task and decomposes it
    resp = await client.post(
        "/v1/market/tasks",
        json={
            "title": "Escalation test parent",
            "description": "Test auto-escalation",
            "bounty_shl": 30,
        },
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    parent_id = resp.json()["task_id"]

    resp = await client.post(
        f"/v1/market/tasks/{parent_id}/decompose",
        json={"subtasks": [
            {"title": "Sub A", "description": "Part A"},
            {"title": "Sub B", "description": "Part B"},
        ]},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201

    # Get subtask IDs
    resp = await client.get(f"/v1/market/tasks/{parent_id}/subtasks")
    assert resp.status_code == 200
    subtasks = resp.json()["subtasks"]
    assert len(subtasks) == 2
    subtask_id = subtasks[0]["task_id"]
    original_bounty = subtasks[0]["bounty_shl"]

    # Set updated_at to 25+ hours ago so escalation triggers
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE tasks SET updated_at = datetime('now', '-25 hours') WHERE task_id = ?",
            (subtask_id,),
        )
        await db.commit()

        # Run escalation
        escalated = await escalate_stuck_subtasks(db)
        assert escalated >= 1

    # Verify bounty increased
    resp = await client.get(f"/v1/market/tasks/{subtask_id}")
    assert resp.status_code == 200
    new_bounty = resp.json()["bounty_shl"]
    assert new_bounty > original_bounty


# ── expire_overdue_tasks ──


async def test_expire_overdue_tasks(client):
    """Tasks past their deadline get expired with bounty refunded."""
    from app.background.tasks import expire_overdue_tasks

    # Carol creates a task
    wallet_before = (await client.get("/v1/market/wallet", headers=auth(state["carol_key"]))).json()
    balance_before = wallet_before["balance_shl"]

    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Expire test", "description": "Will expire", "bounty_shl": 10},
        headers=auth(state["carol_key"]),
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # Verify bounty was deducted
    wallet_mid = (await client.get("/v1/market/wallet", headers=auth(state["carol_key"]))).json()
    assert wallet_mid["balance_shl"] == balance_before - 10

    # Set deadline to the past
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE tasks SET deadline = datetime('now', '-1 hour') WHERE task_id = ?",
            (task_id,),
        )
        await db.commit()

    # Run expiration
    await expire_overdue_tasks()

    # Verify task is expired
    resp = await client.get(f"/v1/market/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"

    # Verify bounty refunded (no fee for expiration)
    wallet_after = (await client.get("/v1/market/wallet", headers=auth(state["carol_key"]))).json()
    assert wallet_after["balance_shl"] == balance_before
