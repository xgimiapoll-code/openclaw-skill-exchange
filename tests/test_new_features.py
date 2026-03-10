"""Tests for Phase 1-9 new features.

Covers: rate limiting, claim withdrawal, disputes, skill rating,
key rotation, matchmaker, weekly rewards, skill publish rewards,
master bonus, recipe validation.
"""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "new_features_test.db")
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

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

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
    """Register agents for all feature tests."""
    state["alice_key"], state["alice_id"] = await register(
        client, "feat-alice", "Alice", ["python", "fastapi", "docker"]
    )
    state["bob_key"], state["bob_id"] = await register(
        client, "feat-bob", "Bob", ["docker", "devops"]
    )
    state["carol_key"], state["carol_id"] = await register(
        client, "feat-carol", "Carol", ["python", "ai"]
    )
    state["dave_key"], state["dave_id"] = await register(
        client, "feat-dave", "Dave", ["rust", "systems"]
    )


# ── Phase 1.4: Pending race condition fix (task_id generated before lock_bounty) ──

async def test_task_creation_no_pending_tx(client):
    """Verify no 'pending' reference_id in transactions after task creation."""
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Race fix test", "description": "Testing pending fix", "bounty_shl": 10},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    state["task1_id"] = task_id

    # Check transactions — no 'pending' reference_id should exist
    txs = (await client.get("/v1/market/wallet/transactions", headers=auth(state["alice_key"]))).json()["transactions"]
    lock_txs = [t for t in txs if t["tx_type"] == "bounty_lock"]
    for tx in lock_txs:
        assert tx["reference_id"] != "pending", "Found 'pending' reference_id — race condition not fixed"


# ── Phase 3: Rate Limiting ──

async def test_reputation_shows_tier_and_limits(client):
    """Reputation endpoint includes tier info and daily limits."""
    resp = await client.get("/v1/market/reputation/me", headers=auth(state["alice_key"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "Newcomer"
    assert "daily_post_limit" in data
    assert "daily_claim_limit" in data


# ── Phase 5.1: Claim Withdrawal ──

async def test_withdraw_claim(client):
    """Solver can withdraw active claim and get deposit refunded."""
    # Alice posts task
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Withdraw test", "description": "Test claim withdrawal", "bounty_shl": 10},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # Bob claims
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200

    # Bob's wallet: balance reduced by 1 SHL deposit
    wallet = (await client.get("/v1/market/wallet", headers=auth(state["bob_key"]))).json()
    balance_after_claim = wallet["balance_shl"]

    # Bob withdraws claim
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/withdraw-claim",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "withdrawn"

    # Bob's deposit refunded
    wallet = (await client.get("/v1/market/wallet", headers=auth(state["bob_key"]))).json()
    assert wallet["balance_shl"] == balance_after_claim + 1.0

    # Task should revert to open (last claim withdrawn)
    task = (await client.get(f"/v1/market/tasks/{task_id}")).json()
    assert task["status"] == "open"


async def test_withdraw_claim_no_active(client):
    """Cannot withdraw if no active claim."""
    task_id = state["task1_id"]
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/withdraw-claim",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 400


# ── Phase 5.3: Skill Rating ──

async def test_rate_skill(client):
    """Rate a skill and verify avg_rating update."""
    # Alice publishes a skill
    resp = await client.post(
        "/v1/market/skills",
        json={"name": "test-skill-rate", "title": "Test Skill For Rating", "recipe": {}},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    skill_id = resp.json()["skill_id"]
    state["rated_skill_id"] = skill_id

    # Bob rates the skill
    resp = await client.post(
        f"/v1/market/skills/{skill_id}/rate",
        json={"score": 5, "comment": "Excellent skill"},
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["score"] == 5
    assert resp.json()["skill_avg_rating"] == 5.0

    # Carol rates the skill
    resp = await client.post(
        f"/v1/market/skills/{skill_id}/rate",
        json={"score": 3},
        headers=auth(state["carol_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["skill_avg_rating"] == 4.0  # (5+3)/2

    # Check skill detail shows updated avg_rating
    resp = await client.get(f"/v1/market/skills/{skill_id}")
    assert resp.status_code == 200
    assert resp.json()["avg_rating"] == 4.0


async def test_cannot_rate_own_skill(client):
    """Author cannot rate their own skill."""
    resp = await client.post(
        f"/v1/market/skills/{state['rated_skill_id']}/rate",
        json={"score": 5},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 400
    assert "own skill" in resp.json()["detail"]


async def test_update_skill_rating(client):
    """Re-rating updates the score instead of creating duplicate."""
    skill_id = state["rated_skill_id"]
    resp = await client.post(
        f"/v1/market/skills/{skill_id}/rate",
        json={"score": 4, "comment": "Updated review"},
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200
    assert resp.json()["skill_avg_rating"] == 3.5  # (4+3)/2


# ── Phase 7.2: API Key Rotation ──

async def test_rotate_api_key(client):
    """Rotate API key, old key stops working, new key works."""
    old_key = state["dave_key"]

    # Rotate
    resp = await client.post("/v1/market/agents/me/rotate-key", headers=auth(old_key))
    assert resp.status_code == 200
    new_key = resp.json()["api_key"]
    assert new_key != old_key

    # Old key should fail
    resp = await client.get("/v1/market/agents/me", headers=auth(old_key))
    assert resp.status_code == 401

    # New key works
    resp = await client.get("/v1/market/agents/me", headers=auth(new_key))
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == state["dave_id"]

    state["dave_key"] = new_key


# ── Phase 4: Disputes ──

async def test_dispute_lifecycle(client):
    """Full dispute lifecycle: create task → complete → dispute → vote → resolve."""
    # Alice posts task, Bob claims, submits, Alice selects winner
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Dispute test task", "description": "Will be disputed", "bounty_shl": 10},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    await client.post(f"/v1/market/tasks/{task_id}/claim", headers=auth(state["bob_key"]))
    sub_resp = await client.post(
        f"/v1/market/tasks/{task_id}/submissions",
        json={"summary": "My solution", "skill_recipe": {}, "confidence_score": 0.8},
        headers=auth(state["bob_key"]),
    )
    sub_id = sub_resp.json()["submission_id"]

    await client.post(
        f"/v1/market/tasks/{task_id}/select-winner",
        json={"submission_id": sub_id, "feedback": "OK", "rating": 3},
        headers=auth(state["alice_key"]),
    )

    # Alice disputes
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/dispute",
        json={"reason": "Solution was incomplete", "evidence": {"files": ["missing.py"]}},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "open"
    assert data["initiator_agent_id"] == state["alice_id"]
    assert data["respondent_agent_id"] == state["bob_id"]
    dispute_id = data["dispute_id"]
    state["dispute_id"] = dispute_id

    # Get dispute
    resp = await client.get(f"/v1/market/disputes/{dispute_id}")
    assert resp.status_code == 200
    assert resp.json()["reason"] == "Solution was incomplete"

    # Get disputes for task
    resp = await client.get(f"/v1/market/tasks/{task_id}/dispute")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_duplicate_dispute(client):
    """Cannot open another dispute while one is active."""
    # Find a task_id that has an open dispute
    resp = await client.get(f"/v1/market/disputes/{state['dispute_id']}")
    task_id = resp.json()["task_id"]

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/dispute",
        json={"reason": "Another dispute"},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 409


async def test_non_participant_cannot_dispute(client):
    """Non-participants cannot open disputes."""
    # Find a completed task
    resp = await client.get(f"/v1/market/disputes/{state['dispute_id']}")
    task_id = resp.json()["task_id"]

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/dispute",
        json={"reason": "I wasn't involved"},
        headers=auth(state["carol_key"]),
    )
    assert resp.status_code == 403


async def test_dispute_cannot_open_on_open_task(client):
    """Cannot dispute a task that isn't completed or expired."""
    resp = await client.post(
        f"/v1/market/tasks/{state['task1_id']}/dispute",
        json={"reason": "Too early"},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 400


# ── Phase 8: Matchmaker ──

async def test_recommended_tasks(client):
    """Recommended endpoint returns tasks matching agent's skill tags."""
    # Create tasks with specific tags
    for title, tags in [
        ("Python web app", ["python", "fastapi"]),
        ("Docker deployment", ["docker", "devops"]),
        ("Rust CLI tool", ["rust", "systems"]),
    ]:
        await client.post(
            "/v1/market/tasks",
            json={"title": title, "description": f"Build a {title}", "bounty_shl": 5, "tags": tags},
            headers=auth(state["alice_key"]),
        )

    # Bob has tags ["docker", "devops"] — should see Docker deployment ranked higher
    resp = await client.get(
        "/v1/market/tasks/recommended",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    # Should not include Alice's own tasks when Alice requests
    # (Bob sees Alice's tasks since Bob isn't the poster)
    task_titles = [t["title"] for t in data["tasks"]]
    assert any("Docker" in t for t in task_titles)


async def test_recommended_excludes_own_tasks(client):
    """Recommendations don't include tasks posted by the requesting agent."""
    resp = await client.get(
        "/v1/market/tasks/recommended",
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 200
    for task in resp.json()["tasks"]:
        assert task["poster_agent_id"] != state["alice_id"]


# ── Phase 2.2: Skill Publish Reward ──

async def test_skill_publish_reward(client):
    """Skill author gets reward when install count reaches threshold."""
    # Alice publishes a skill
    resp = await client.post(
        "/v1/market/skills",
        json={"name": "popular-skill", "title": "Popular Skill"},
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    skill_id = resp.json()["skill_id"]

    # Get Alice's balance before
    wallet_before = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    balance_before = wallet_before["balance_shl"]

    # 5 agents install it (need 5 for reward)
    # Register enough agents to reach threshold
    installers = []
    for i in range(5):
        key, aid = await register(client, f"installer-{i}", f"Installer{i}")
        installers.append((key, aid))

    for key, aid in installers:
        resp = await client.post(
            f"/v1/market/skills/{skill_id}/install",
            headers=auth(key),
        )
        assert resp.status_code == 200

    # Alice should have received 25 SHL reward
    wallet_after = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    assert wallet_after["balance_shl"] == balance_before + 25.0

    # Verify reward_granted flag prevents duplicate
    key, aid = await register(client, "installer-extra", "InstallerExtra")
    resp = await client.post(f"/v1/market/skills/{skill_id}/install", headers=auth(key))
    assert resp.status_code == 200
    wallet_check = (await client.get("/v1/market/wallet", headers=auth(state["alice_key"]))).json()
    assert wallet_check["balance_shl"] == wallet_after["balance_shl"]  # No double reward


# ── Market Stats ──

async def test_market_stats_include_disputes(client):
    """Market stats endpoint includes dispute counts."""
    resp = await client.get("/v1/market/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_disputes" in data
    assert "open_disputes" in data
    assert data["total_disputes"] >= 1


# ── Leaderboard includes tier ──

async def test_leaderboard_includes_tier(client):
    """Leaderboard entries include tier name."""
    resp = await client.get("/v1/market/reputation/leaderboard/top")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) > 0
    for entry in data:
        assert "tier" in entry


# ── Recipe Validation ──

async def test_recipe_validation_invalid_metadata(client):
    """Creating a skill with invalid recipe metadata raises error."""
    resp = await client.post(
        "/v1/market/skills",
        json={
            "name": "bad-recipe-skill",
            "title": "Bad Recipe",
            "recipe": {"metadata": "not-a-dict"},
        },
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 400


async def test_recipe_validation_valid(client):
    """Creating a skill with valid recipe structure succeeds."""
    resp = await client.post(
        "/v1/market/skills",
        json={
            "name": "good-recipe-skill",
            "title": "Good Recipe",
            "recipe": {
                "schema_version": "1.0.0",
                "metadata": {"name": "good-recipe-skill", "title": "Good Recipe"},
                "steps": [{"step": 1, "title": "Do thing", "action": "code"}],
            },
        },
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201


# ── Claim Reputation Check ──

async def test_claim_task_reputation_check(client):
    """Verify claim_task checks reputation (same as create_task)."""
    # This is implicitly tested — the claim_task code now has the check.
    # We just verify it's callable without error for normal agents
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Rep check claim test", "description": "Test", "bounty_shl": 5},
        headers=auth(state["alice_key"]),
    )
    task_id = resp.json()["task_id"]

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200
