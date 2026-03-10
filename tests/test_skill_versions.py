"""Tests for skill version management."""

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
async def author(client):
    r = await client.post("/v1/market/agents/register", json={
        "node_id": "skill-version-author",
        "display_name": "Version Author",
        "skill_tags": ["python", "docker"],
    })
    assert r.status_code == 201
    return r.json()


@pytest.fixture(scope="module")
async def user(client):
    r = await client.post("/v1/market/agents/register", json={
        "node_id": "skill-version-user",
        "display_name": "Version User",
        "skill_tags": ["python"],
    })
    assert r.status_code == 201
    return r.json()


@pytest.fixture(scope="module")
async def skill_v1(client, author):
    """Publish initial skill v1.0.0."""
    r = await client.post("/v1/market/skills", json={
        "name": "versioned-skill",
        "title": "Versioned Skill v1",
        "description": "First version",
        "tags": ["python"],
        "recipe": {"steps": [{"step": 1, "title": "Init", "action": "run"}]},
    }, headers={"Authorization": f"Bearer {author['api_key']}"})
    assert r.status_code == 201
    return r.json()


async def test_publish_new_version(client, author, skill_v1):
    """Publish v1.1.0 of an existing skill."""
    r = await client.post(f"/v1/market/skills/{skill_v1['skill_id']}/versions", json={
        "version": "1.1.0",
        "title": "Versioned Skill v1.1",
        "description": "Improved version",
    }, headers={"Authorization": f"Bearer {author['api_key']}"})
    assert r.status_code == 201
    data = r.json()
    assert data["version"] == "1.1.0"
    assert data["name"] == "versioned-skill"
    assert data["skill_id"] != skill_v1["skill_id"]  # New UUID


async def test_list_versions(client, skill_v1):
    """List all versions of a skill."""
    r = await client.get(f"/v1/market/skills/{skill_v1['skill_id']}/versions")
    assert r.status_code == 200
    versions = r.json()
    assert len(versions) >= 2
    version_strings = [v["version"] for v in versions]
    assert "1.0.0" in version_strings
    assert "1.1.0" in version_strings


async def test_get_specific_version(client, skill_v1):
    """Get a specific version of a skill."""
    r = await client.get(f"/v1/market/skills/{skill_v1['skill_id']}/versions/1.0.0")
    assert r.status_code == 200
    assert r.json()["version"] == "1.0.0"

    r = await client.get(f"/v1/market/skills/{skill_v1['skill_id']}/versions/1.1.0")
    assert r.status_code == 200
    assert r.json()["version"] == "1.1.0"


async def test_get_nonexistent_version(client, skill_v1):
    """404 for nonexistent version."""
    r = await client.get(f"/v1/market/skills/{skill_v1['skill_id']}/versions/9.9.9")
    assert r.status_code == 404


async def test_duplicate_version(client, author, skill_v1):
    """Reject duplicate version number."""
    r = await client.post(f"/v1/market/skills/{skill_v1['skill_id']}/versions", json={
        "version": "1.0.0",
    }, headers={"Authorization": f"Bearer {author['api_key']}"})
    assert r.status_code == 400
    assert "already exists" in r.json()["detail"]


async def test_invalid_version_format(client, author, skill_v1):
    """Reject invalid version format."""
    r = await client.post(f"/v1/market/skills/{skill_v1['skill_id']}/versions", json={
        "version": "v2",
    }, headers={"Authorization": f"Bearer {author['api_key']}"})
    assert r.status_code == 400


async def test_non_author_cannot_publish(client, user, skill_v1):
    """Only original author can publish new versions."""
    r = await client.post(f"/v1/market/skills/{skill_v1['skill_id']}/versions", json={
        "version": "2.0.0",
    }, headers={"Authorization": f"Bearer {user['api_key']}"})
    assert r.status_code == 400
    assert "author" in r.json()["detail"].lower()


async def test_install_specific_version(client, user, skill_v1):
    """Install a specific version of a skill."""
    r = await client.post(
        f"/v1/market/skills/{skill_v1['skill_id']}/install",
        json={"version": "1.0.0"},
        headers={"Authorization": f"Bearer {user['api_key']}"},
    )
    assert r.status_code == 200
    assert r.json()["installed_version"] == "1.0.0"
