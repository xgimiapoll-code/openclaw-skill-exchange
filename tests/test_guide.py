"""Tests for agent-friendly guide endpoints: onboarding, playbook, for-me, dashboard."""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "guide_test.db")
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
    """Register test agents."""
    state["alice_key"], state["alice_id"] = await register(
        client, "guide-alice", "Alice", ["python", "fastapi", "docker"]
    )
    state["bob_key"], state["bob_id"] = await register(
        client, "guide-bob", "Bob", ["docker", "devops"]
    )


# ── Onboarding (no auth) ──


async def test_onboarding_no_auth(client):
    """Onboarding endpoint works without authentication."""
    resp = await client.get("/v1/market/onboarding")
    assert resp.status_code == 200
    data = resp.json()
    assert "what_is_this" in data
    assert "how_to_earn" in data
    assert isinstance(data["how_to_earn"], list)
    assert len(data["how_to_earn"]) >= 3
    assert "earning_strategies" in data
    assert len(data["earning_strategies"]) >= 3
    assert "market_snapshot" in data
    snap = data["market_snapshot"]
    assert "open_tasks" in snap
    assert "active_agents" in snap
    assert snap["active_agents"] >= 2  # alice + bob


async def test_onboarding_strategies_have_action(client):
    """Each earning strategy includes a concrete API action."""
    resp = await client.get("/v1/market/onboarding")
    data = resp.json()
    for strategy in data["earning_strategies"]:
        assert "action" in strategy
        assert "risk" in strategy
        assert "yield" in strategy


# ── Playbook (no auth) ──


async def test_playbook_no_auth(client):
    """Playbook works without auth and returns all strategies."""
    resp = await client.get("/v1/market/playbook")
    assert resp.status_code == 200
    data = resp.json()
    assert "passive_income" in data
    assert "freelancer" in data
    assert "skill_publisher" in data
    assert "architect" in data
    assert "combined" in data


async def test_playbook_has_steps(client):
    """Each strategy includes actionable steps."""
    resp = await client.get("/v1/market/playbook")
    data = resp.json()
    for key in ("passive_income", "freelancer", "skill_publisher", "architect"):
        assert "steps" in data[key], f"{key} missing steps"
        assert len(data[key]["steps"]) >= 2, f"{key} needs at least 2 steps"
    assert "daily_routine" in data["combined"]


# ── Tasks For Me (auth required) ──


async def test_for_me_empty_market(client):
    """For-me returns empty when no tasks exist."""
    resp = await client.get("/v1/market/tasks/for-me", headers=auth(state["alice_key"]))
    assert resp.status_code == 200
    data = resp.json()
    assert "your_skills" in data
    assert "matching_tasks" in data
    assert data["your_skills"] == ["python", "fastapi", "docker"]
    assert isinstance(data["matching_tasks"], list)


async def test_for_me_with_tasks(client):
    """For-me recommends tasks with profit estimation."""
    # Alice posts a task (Bob should see it)
    resp = await client.post(
        "/v1/market/tasks",
        json={
            "title": "Build Docker CI pipeline",
            "description": "Set up Docker-based CI/CD for Python project",
            "bounty_shl": 50,
            "tags": ["docker", "devops", "python"],
            "difficulty": "medium",
        },
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201
    state["task1_id"] = resp.json()["task_id"]

    # Bob checks for-me
    resp = await client.get("/v1/market/tasks/for-me", headers=auth(state["bob_key"]))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["matching_tasks"]) >= 1

    task = data["matching_tasks"][0]
    assert task["task_id"] == state["task1_id"]
    assert task["bounty_shl"] == 50.0
    assert "profit_estimate" in task
    assert task["profit_estimate"]["net_profit_shl"] > 0
    assert "competition" in task
    assert task["competition"]["slots_remaining"] > 0
    assert "claim_url" in task
    assert task["can_afford"] is True


async def test_for_me_matching_tags(client):
    """For-me shows matching tags for each task."""
    resp = await client.get("/v1/market/tasks/for-me", headers=auth(state["bob_key"]))
    data = resp.json()
    task = data["matching_tasks"][0]
    # Bob has docker, devops — task has docker, devops, python
    assert "docker" in task["matching_tags"]
    assert "devops" in task["matching_tags"]
    assert task["match_score"] >= 2


async def test_for_me_excludes_own_tasks(client):
    """For-me doesn't show tasks posted by the agent."""
    resp = await client.get("/v1/market/tasks/for-me", headers=auth(state["alice_key"]))
    data = resp.json()
    task_ids = [t["task_id"] for t in data["matching_tasks"]]
    assert state["task1_id"] not in task_ids


async def test_for_me_no_auth_fails(client):
    """For-me requires authentication."""
    resp = await client.get("/v1/market/tasks/for-me")
    assert resp.status_code in (401, 403)


# ── Dashboard (auth required) ──


async def test_dashboard_initial(client):
    """Dashboard shows initial state correctly."""
    resp = await client.get("/v1/market/my-dashboard", headers=auth(state["bob_key"]))
    assert resp.status_code == 200
    data = resp.json()

    assert data["agent_id"] == state["bob_id"]
    assert data["balance_shl"] == 100.0
    assert "reputation" in data
    assert data["reputation"]["tier"] == "Newcomer"
    assert "stats" in data
    assert "active_claims" in data
    assert "pending_submissions" in data
    assert "faucet" in data
    assert "suggested_next_action" in data


async def test_dashboard_faucet_available(client):
    """Dashboard shows faucet as available when not claimed today."""
    resp = await client.get("/v1/market/my-dashboard", headers=auth(state["bob_key"]))
    data = resp.json()
    assert data["faucet"]["available"] is True
    assert data["faucet"]["action"] is not None


async def test_dashboard_after_claim(client):
    """Dashboard shows active claims after claiming a task."""
    # Bob claims Alice's task
    resp = await client.post(
        f"/v1/market/tasks/{state['task1_id']}/claim",
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 200

    resp = await client.get("/v1/market/my-dashboard", headers=auth(state["bob_key"]))
    data = resp.json()
    assert len(data["active_claims"]) == 1
    claim = data["active_claims"][0]
    assert claim["task_id"] == state["task1_id"]
    assert claim["claim_status"] == "active"
    assert "action_needed" in claim
    assert "submissions" in claim["action_needed"]


async def test_dashboard_suggested_action_work_first(client):
    """Suggested action prioritizes active claims over faucet."""
    resp = await client.get("/v1/market/my-dashboard", headers=auth(state["bob_key"]))
    data = resp.json()
    suggested = data["suggested_next_action"]
    assert suggested["priority"] == "high"
    assert "待完成" in suggested["message"]


async def test_dashboard_poster_needs_review(client):
    """Dashboard shows poster when tasks need winner selection."""
    # Bob submits solution
    resp = await client.post(
        f"/v1/market/tasks/{state['task1_id']}/submissions",
        json={"summary": "Built the pipeline", "confidence_score": 0.9},
        headers=auth(state["bob_key"]),
    )
    assert resp.status_code == 201

    # Alice's dashboard should show task needing review
    resp = await client.get("/v1/market/my-dashboard", headers=auth(state["alice_key"]))
    data = resp.json()
    assert len(data["tasks_needing_review"]) == 1
    assert data["suggested_next_action"]["priority"] == "high"
    assert "评选" in data["suggested_next_action"]["message"]


async def test_dashboard_suggested_tasks(client):
    """Dashboard includes top task suggestions."""
    # Post another task for Bob to see
    resp = await client.post(
        "/v1/market/tasks",
        json={
            "title": "Deploy Kubernetes cluster",
            "description": "Set up k8s on cloud",
            "bounty_shl": 30,
            "tags": ["devops", "kubernetes"],
        },
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 201

    resp = await client.get("/v1/market/my-dashboard", headers=auth(state["bob_key"]))
    data = resp.json()
    assert "suggested_tasks" in data
    # Bob should see the k8s task (matches devops tag)
    assert len(data["suggested_tasks"]) >= 1


async def test_dashboard_no_auth_fails(client):
    """Dashboard requires authentication."""
    resp = await client.get("/v1/market/my-dashboard")
    assert resp.status_code in (401, 403)
