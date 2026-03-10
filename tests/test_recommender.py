"""Tests for multi-factor recommendation engine."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

BASE = "http://test"


@pytest.fixture(scope="module")
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        yield c


@pytest.fixture(scope="module")
async def poster(client):
    r = await client.post("/v1/market/agents/register", json={
        "node_id": "rec-poster",
        "display_name": "Rec Poster",
        "skill_tags": ["devops"],
    })
    assert r.status_code == 201
    return r.json()


@pytest.fixture(scope="module")
async def solver(client):
    r = await client.post("/v1/market/agents/register", json={
        "node_id": "rec-solver",
        "display_name": "Rec Solver",
        "skill_tags": ["python", "fastapi", "docker"],
    })
    assert r.status_code == 201
    return r.json()


@pytest.fixture(scope="module")
async def tasks_created(client, poster):
    """Create diverse tasks for testing recommendations."""
    headers = {"Authorization": f"Bearer {poster['api_key']}"}
    task_ids = []
    for task_data in [
        {"title": "Python API", "description": "Build a REST API", "bounty_shl": 10,
         "tags": ["python", "fastapi"], "category": "backend", "difficulty": "easy"},
        {"title": "Docker Setup", "description": "Containerize app", "bounty_shl": 8,
         "tags": ["docker", "devops"], "category": "devops", "difficulty": "medium"},
        {"title": "React Dashboard", "description": "Build UI", "bounty_shl": 15,
         "tags": ["react", "typescript"], "category": "frontend", "difficulty": "hard"},
        {"title": "ML Pipeline", "description": "Train model", "bounty_shl": 20,
         "tags": ["python", "ml"], "category": "ai-ml", "difficulty": "expert"},
    ]:
        r = await client.post("/v1/market/tasks", json=task_data, headers=headers)
        assert r.status_code == 201
        task_ids.append(r.json()["task_id"])
    return task_ids


async def test_task_recommendations_tag_match(client, solver, tasks_created):
    """Tasks matching agent's tags should rank higher."""
    headers = {"Authorization": f"Bearer {solver['api_key']}"}
    r = await client.get("/v1/market/tasks/recommended", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 4

    # Python/fastapi/docker tasks should rank higher than react
    task_titles = [t["title"] for t in data["tasks"]]
    # The React Dashboard (no tag overlap) should be last
    react_idx = task_titles.index("React Dashboard") if "React Dashboard" in task_titles else -1
    python_idx = task_titles.index("Python API") if "Python API" in task_titles else -1
    if react_idx >= 0 and python_idx >= 0:
        assert python_idx < react_idx


async def test_skill_recommendations(client, solver, poster):
    """Skill recommendations exclude already-installed skills."""
    # Create a skill
    headers_poster = {"Authorization": f"Bearer {poster['api_key']}"}
    r = await client.post("/v1/market/skills", json={
        "name": "rec-test-skill",
        "title": "Recommended Skill",
        "description": "For recommendation testing",
        "tags": ["python", "fastapi"],
    }, headers=headers_poster)
    assert r.status_code == 201
    skill_id = r.json()["skill_id"]

    headers_solver = {"Authorization": f"Bearer {solver['api_key']}"}

    # Before installing, should appear in recommendations
    r = await client.get("/v1/market/skills/recommended", headers=headers_solver)
    assert r.status_code == 200
    rec_ids = [s["skill_id"] for s in r.json()["skills"]]
    assert skill_id in rec_ids

    # Install the skill
    r = await client.post(f"/v1/market/skills/{skill_id}/install", headers=headers_solver)
    assert r.status_code == 200

    # After installing, should NOT appear in recommendations
    r = await client.get("/v1/market/skills/recommended", headers=headers_solver)
    assert r.status_code == 200
    rec_ids = [s["skill_id"] for s in r.json()["skills"]]
    assert skill_id not in rec_ids


async def test_recommendations_exclude_own_tasks(client, poster, tasks_created):
    """Poster shouldn't see their own tasks in recommendations."""
    headers = {"Authorization": f"Bearer {poster['api_key']}"}
    r = await client.get("/v1/market/tasks/recommended", headers=headers)
    assert r.status_code == 200
    for task in r.json()["tasks"]:
        assert task["poster_agent_id"] != poster["agent"]["agent_id"]
