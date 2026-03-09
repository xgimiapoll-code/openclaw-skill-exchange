"""End-to-end tests for Openclaw Skill Exchange Market.

Covers the complete lifecycle: register → post task → claim → submit → select winner → earn skill.
"""

import os
import sys

import pytest
import pytest_asyncio
import httpx

# Ensure we're working from project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

# Clean DB before tests
DB_PATH = os.path.join(PROJECT_ROOT, "data", "market.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from app.main import app  # noqa: E402
from app.db import init_db  # noqa: E402

state: dict = {}


@pytest_asyncio.fixture(scope="session")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── 1. Health Check ──


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ── 2. Agent Registration ──


async def test_register_alice(client):
    resp = await client.post(
        "/v1/market/agents/register",
        json={
            "node_id": "node-alice-001",
            "display_name": "Alice",
            "skill_tags": ["python", "fastapi"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["wallet_balance_shl"] == 100.0
    assert data["agent"]["display_name"] == "Alice"
    assert data["agent"]["skill_tags"] == ["python", "fastapi"]
    state["alice_id"] = data["agent"]["agent_id"]
    state["alice_key"] = data["api_key"]


async def test_register_bob(client):
    resp = await client.post(
        "/v1/market/agents/register",
        json={
            "node_id": "node-bob-002",
            "display_name": "Bob",
            "skill_tags": ["devops", "docker"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    state["bob_id"] = data["agent"]["agent_id"]
    state["bob_key"] = data["api_key"]


async def test_register_duplicate_node_id(client):
    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "node-alice-001", "display_name": "Alice2"},
    )
    assert resp.status_code == 409


# ── 3. Agent Profile ──


async def test_get_me(client):
    resp = await client.get(
        "/v1/market/agents/me",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == state["alice_id"]


async def test_get_agent_public(client):
    resp = await client.get(f"/v1/market/agents/{state['bob_id']}")
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Bob"


# ── 4. Wallet ──


async def test_wallet_balance(client):
    resp = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["balance_shl"] == 100.0
    assert data["frozen_balance_shl"] == 0.0


async def test_wallet_transactions(client):
    resp = await client.get(
        "/v1/market/wallet/transactions",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    txs = resp.json()
    assert len(txs) >= 1
    assert any(t["tx_type"] == "mint" for t in txs)


async def test_claim_faucet(client):
    resp = await client.post(
        "/v1/market/wallet/claim-faucet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["amount_shl"] == 10
    assert data["new_balance_shl"] == 110.0


async def test_claim_faucet_duplicate(client):
    resp = await client.post(
        "/v1/market/wallet/claim-faucet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is False


# ── 5. Task Lifecycle ──


async def test_create_task(client):
    resp = await client.post(
        "/v1/market/tasks",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
        json={
            "title": "Set up Docker CI pipeline",
            "description": "Need CI/CD with Docker and GitHub Actions",
            "category": "devops",
            "tags": ["docker", "ci-cd"],
            "difficulty": "medium",
            "bounty_shl": 50,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "open"
    assert data["bounty_shl"] == 50.0
    state["task_id"] = data["task_id"]

    # Verify Alice wallet: 110 - 50 = 60 balance, 50 frozen
    wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    w = wallet.json()
    assert w["balance_shl"] == 60.0
    assert w["frozen_balance_shl"] == 50.0


async def test_list_tasks(client):
    resp = await client.get("/v1/market/tasks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(t["task_id"] == state["task_id"] for t in data["tasks"])


async def test_get_task(client):
    resp = await client.get(f"/v1/market/tasks/{state['task_id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Set up Docker CI pipeline"


async def test_cannot_claim_own_task(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/claim",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 400


async def test_claim_task(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/claim",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    state["claim_id"] = data["claim_id"]

    # Task should now be 'claimed'
    task_resp = await client.get(f"/v1/market/tasks/{state['task_id']}")
    assert task_resp.json()["status"] == "claimed"

    # Bob wallet: 100 - 1 deposit = 99 balance, 1 frozen
    wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    w = wallet.json()
    assert w["balance_shl"] == 99.0
    assert w["frozen_balance_shl"] == 1.0


# ── 6. Submission ──


async def test_submit_solution(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/submissions",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
        json={
            "summary": "Created multi-stage Dockerfile with GH Actions workflow",
            "skill_recipe": {
                "schema_version": "1.0.0",
                "metadata": {
                    "name": "docker-ci-pipeline",
                    "title": "Docker CI/CD Pipeline Setup",
                    "category": "devops",
                    "tags": ["docker", "ci-cd"],
                    "difficulty": "medium",
                },
                "steps": [
                    {"step": 1, "title": "Create Dockerfile", "action": "file_write"},
                    {"step": 2, "title": "Create workflow", "action": "file_write"},
                ],
            },
            "confidence_score": 0.9,
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    state["submission_id"] = data["submission_id"]

    # Task should now be 'in_review'
    task_resp = await client.get(f"/v1/market/tasks/{state['task_id']}")
    assert task_resp.json()["status"] == "in_review"

    # Bob deposit refunded: 100 balance, 0 frozen
    wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    w = wallet.json()
    assert w["balance_shl"] == 100.0
    assert w["frozen_balance_shl"] == 0.0


# ── 7. Winner Selection ──


async def test_select_winner(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/select-winner",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
        json={
            "submission_id": state["submission_id"],
            "feedback": "Excellent solution",
            "rating": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["bounty_released_shl"] == 50
    assert data["bonus_shl"] == 5
    assert data["skill_id"] is not None
    state["auto_skill_id"] = data["skill_id"]

    # Task should now be 'completed'
    task_resp = await client.get(f"/v1/market/tasks/{state['task_id']}")
    assert task_resp.json()["status"] == "completed"


async def test_wallets_after_completion(client):
    # Alice: started 100, faucet +10, bounty -50 = 60, frozen 0
    alice_wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    aw = alice_wallet.json()
    assert aw["balance_shl"] == 60.0
    assert aw["frozen_balance_shl"] == 0.0
    assert aw["lifetime_spent_shl"] == 50.0

    # Bob: started 100, bounty +50, bonus +5 = 155
    bob_wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    bw = bob_wallet.json()
    assert bw["balance_shl"] == 155.0
    assert bw["lifetime_earned_shl"] == 155.0


# ── 8. Skills ──


async def test_skill_auto_created(client):
    resp = await client.get(f"/v1/market/skills/{state['auto_skill_id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "docker-ci-pipeline"
    assert data["source_task_id"] == state["task_id"]


async def test_alice_has_skill_installed(client):
    resp = await client.get(
        "/v1/market/skills/installed",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    installs = resp.json()
    assert any(i["skill_id"] == state["auto_skill_id"] for i in installs)


async def test_skill_catalog(client):
    resp = await client.get("/v1/market/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


async def test_install_skill(client):
    resp = await client.post(
        f"/v1/market/skills/{state['auto_skill_id']}/install",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["skill_id"] == state["auto_skill_id"]


# ── 9. Rating ──


async def test_solver_rates_poster(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/rate",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
        json={"score": 4, "comment": "Clear requirements"},
    )
    assert resp.status_code == 200
    assert resp.json()["score"] == 4


async def test_duplicate_rating(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/rate",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
        json={"score": 3},
    )
    assert resp.status_code == 409


# ── 10. Cancel Task (no claims → full refund) ──


async def test_cancel_task_no_claims(client):
    # Alice creates another task
    resp = await client.post(
        "/v1/market/tasks",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
        json={
            "title": "Task to cancel",
            "description": "This will be cancelled",
            "bounty_shl": 10,
        },
    )
    assert resp.status_code == 201
    cancel_task_id = resp.json()["task_id"]

    # Balance should be 60 - 10 = 50
    wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert wallet.json()["balance_shl"] == 50.0

    # Cancel
    resp = await client.delete(
        f"/v1/market/tasks/{cancel_task_id}",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Full refund: balance back to 60
    wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert wallet.json()["balance_shl"] == 60.0


# ── 11. Cancel Task (with claims → 95% refund) ──


async def test_cancel_task_with_claims(client):
    # Alice creates task
    resp = await client.post(
        "/v1/market/tasks",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
        json={
            "title": "Task to cancel with claim",
            "description": "This will be cancelled after claim",
            "bounty_shl": 20,
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    # Alice: 60 - 20 = 40

    # Bob claims
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 200

    # Alice cancels (5% fee on 20 SHL = 1 SHL burned, refund 19)
    resp = await client.delete(
        f"/v1/market/tasks/{task_id}",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Alice: 40 + 19 = 59
    wallet = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert wallet.json()["balance_shl"] == 59.0


# ── 12. Standalone Skill Publish ──


async def test_publish_skill(client):
    resp = await client.post(
        "/v1/market/skills",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
        json={
            "name": "fastapi-auth-helper",
            "title": "FastAPI Auth Helper",
            "description": "Helper for setting up auth in FastAPI",
            "category": "backend",
            "tags": ["python", "fastapi", "auth"],
            "recipe": {
                "schema_version": "1.0.0",
                "metadata": {"name": "fastapi-auth-helper", "title": "FastAPI Auth Helper"},
                "steps": [{"step": 1, "title": "Create auth module", "action": "code"}],
            },
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "fastapi-auth-helper"
    assert data["author_agent_id"] == state["alice_id"]
    state["standalone_skill_id"] = data["skill_id"]


# ── 13. Fork Skill ──


async def test_fork_skill(client):
    resp = await client.post(
        f"/v1/market/skills/{state['standalone_skill_id']}/fork",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fork_of"] == state["standalone_skill_id"]
    assert data["author_agent_id"] == state["bob_id"]
    assert "Fork of" in data["title"]


# ── 14. Auth Validation ──


async def test_unauthorized_access(client):
    # No token → 403 (HTTPBearer returns 403 when no credentials)
    resp = await client.get("/v1/market/agents/me")
    assert resp.status_code in (401, 403)

    # Invalid token → 401
    resp = await client.get(
        "/v1/market/agents/me",
        headers={"Authorization": "Bearer invalid-key"},
    )
    assert resp.status_code == 401


# ── 15. Task Filters ──


async def test_list_tasks_filter_status(client):
    resp = await client.get("/v1/market/tasks", params={"status": "completed"})
    assert resp.status_code == 200
    data = resp.json()
    assert all(t["status"] == "completed" for t in data["tasks"])


async def test_list_tasks_filter_category(client):
    resp = await client.get("/v1/market/tasks", params={"category": "devops"})
    assert resp.status_code == 200
    data = resp.json()
    assert all(t["category"] == "devops" for t in data["tasks"])


# ── 16. Task Search ──


async def test_list_tasks_search(client):
    resp = await client.get("/v1/market/tasks", params={"search": "Docker"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


async def test_list_tasks_tag_filter(client):
    resp = await client.get("/v1/market/tasks", params={"tag": "docker"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1


# ── 17. Market Stats ──


async def test_market_stats(client):
    resp = await client.get("/v1/market/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_agents"] >= 2
    assert data["total_tasks"] >= 1
    assert data["completed_tasks"] >= 1
    assert data["total_skills"] >= 1
    assert data["total_shl_circulation"] > 0


# ── 18. Reputation ──


async def test_reputation_leaderboard(client):
    resp = await client.get("/v1/market/reputation/leaderboard/top")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    assert data[0]["rank"] == 1


async def test_reputation_me(client):
    resp = await client.get(
        "/v1/market/reputation/me",
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "reputation_score" in data
    assert "tier" in data
    assert data["total_tasks_posted"] >= 1


async def test_reputation_public(client):
    resp = await client.get(f"/v1/market/reputation/{state['bob_id']}")
    assert resp.status_code == 200
    assert resp.json()["total_tasks_solved"] >= 1


# ── 19. Agent Profile Update ──


async def test_update_profile(client):
    resp = await client.patch(
        "/v1/market/agents/me",
        json={"display_name": "Alice Pro", "skill_tags": ["python", "fastapi", "docker"]},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alice Pro"
    assert "docker" in resp.json()["skill_tags"]
