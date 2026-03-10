"""Edge case tests — insufficient balance, multiple solvers, boundary conditions."""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "edge_test.db")
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


async def register(client, node_id, name):
    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": node_id, "display_name": name},
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["api_key"], data["agent"]["agent_id"]


async def test_setup_agents(client):
    """Register 4 agents for edge case tests."""
    state["alice_key"], state["alice_id"] = await register(client, "edge-alice", "Alice")
    state["bob_key"], state["bob_id"] = await register(client, "edge-bob", "Bob")
    state["carol_key"], state["carol_id"] = await register(client, "edge-carol", "Carol")
    state["dave_key"], state["dave_id"] = await register(client, "edge-dave", "Dave")


async def test_insufficient_balance(client):
    """Cannot post task with bounty exceeding balance."""
    resp = await client.post(
        "/v1/market/tasks",
        json={
            "title": "Too expensive",
            "description": "This bounty exceeds balance",
            "bounty_shl": 999,
        },
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    # Rejected by either balance check or tx velocity limit — both valid
    assert "Insufficient" in detail or "limit" in detail.lower()


async def test_multiple_solvers_claim(client):
    """Multiple solvers can claim the same task."""
    # Alice posts task with max_solvers=3
    resp = await client.post(
        "/v1/market/tasks",
        json={
            "title": "Multi-solver task",
            "description": "Up to 3 solvers",
            "bounty_shl": 30,
            "max_solvers": 3,
        },
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]
    state["multi_task_id"] = task_id

    # Bob, Carol, and Dave all claim
    for key in [state["bob_key"], state["carol_key"], state["dave_key"]]:
        resp = await client.post(
            f"/v1/market/tasks/{task_id}/claim",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200


async def test_multiple_submissions(client):
    """Multiple solvers submit, only one wins."""
    task_id = state["multi_task_id"]

    # All three submit
    sub_ids = []
    for key, summary in [
        (state["bob_key"], "Bob's solution"),
        (state["carol_key"], "Carol's solution"),
        (state["dave_key"], "Dave's solution"),
    ]:
        resp = await client.post(
            f"/v1/market/tasks/{task_id}/submissions",
            json={"summary": summary, "skill_recipe": {}, "confidence_score": 0.8},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 201
        sub_ids.append(resp.json()["submission_id"])

    state["multi_sub_ids"] = sub_ids

    # List submissions
    resp = await client.get(f"/v1/market/tasks/{task_id}/submissions")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


async def test_select_one_winner_from_multiple(client):
    """Selecting one winner marks others as rejected/lost."""
    task_id = state["multi_task_id"]
    winning_sub = state["multi_sub_ids"][1]  # Carol wins

    resp = await client.post(
        f"/v1/market/tasks/{task_id}/select-winner",
        json={"submission_id": winning_sub, "feedback": "Best approach", "rating": 5},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["solver_agent_id"] == state["carol_id"]

    # Check task is completed
    task = await client.get(f"/v1/market/tasks/{task_id}")
    assert task.json()["status"] == "completed"

    # Check submissions: 1 accepted, 2 rejected
    subs = await client.get(f"/v1/market/tasks/{task_id}/submissions")
    statuses = [s["status"] for s in subs.json()]
    assert statuses.count("accepted") == 1
    assert statuses.count("rejected") == 2


async def test_cannot_claim_completed_task(client):
    """Cannot claim a completed task."""
    # Register new agent
    key, _ = await register(client, "edge-eve", "Eve")
    resp = await client.post(
        f"/v1/market/tasks/{state['multi_task_id']}/claim",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 400


async def test_cannot_submit_without_claim(client):
    """Cannot submit without claiming first."""
    # Alice posts new task
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "No claim test", "description": "Test", "bounty_shl": 5},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    task_id = resp.json()["task_id"]

    # Bob tries to submit without claiming (task is still 'open', not 'claimed')
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/submissions",
        json={"summary": "No claim", "skill_recipe": {}},
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 400


async def test_double_claim_rejected(client):
    """Same solver cannot claim same task twice."""
    # Alice posts task
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Double claim test", "description": "Test", "bounty_shl": 5},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    task_id = resp.json()["task_id"]

    # Bob claims
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 200

    # Bob tries to claim again
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 400
    assert "Already claimed" in resp.json()["detail"]


async def test_non_poster_cannot_cancel(client):
    """Only poster can cancel a task."""
    # Alice posts task
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Cancel auth test", "description": "Test", "bounty_shl": 5},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    task_id = resp.json()["task_id"]

    # Bob tries to cancel
    resp = await client.delete(
        f"/v1/market/tasks/{task_id}",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    assert resp.status_code == 400
    assert "Only poster" in resp.json()["detail"]


async def test_non_poster_cannot_select_winner(client):
    """Only poster can select winner."""
    # Alice posts task, Bob claims and submits
    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Winner auth test", "description": "Test", "bounty_shl": 5},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    task_id = resp.json()["task_id"]

    await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    sub = await client.post(
        f"/v1/market/tasks/{task_id}/submissions",
        json={"summary": "Solution", "skill_recipe": {}},
        headers={"Authorization": f"Bearer {state['bob_key']}"},
    )
    sub_id = sub.json()["submission_id"]

    # Carol tries to select winner
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/select-winner",
        json={"submission_id": sub_id, "feedback": "Good", "rating": 5},
        headers={"Authorization": f"Bearer {state['carol_key']}"},
    )
    assert resp.status_code == 403


async def test_skill_name_validation(client):
    """Skill name must be lowercase with hyphens."""
    resp = await client.post(
        "/v1/market/skills",
        json={"name": "Invalid Name!", "title": "Test"},
        headers={"Authorization": f"Bearer {state['alice_key']}"},
    )
    assert resp.status_code == 422  # Pydantic validation error


async def test_task_not_found(client):
    """Get non-existent task returns 404."""
    resp = await client.get("/v1/market/tasks/non-existent-id")
    assert resp.status_code == 404


async def test_skill_not_found(client):
    """Get non-existent skill returns 404."""
    resp = await client.get("/v1/market/skills/non-existent-id")
    assert resp.status_code == 404
