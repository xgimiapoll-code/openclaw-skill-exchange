"""Tests for MCP manifest, llms.txt, and skill.md discovery endpoints."""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "mcp_test.db")
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


@pytest_asyncio.fixture(scope="module")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── MCP Manifest ──


async def test_mcp_manifest_structure(client):
    """MCP manifest has required fields."""
    resp = await client.get("/.well-known/mcp.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["schema_version"] == "1.0"
    assert data["name"] == "openclaw-skill-exchange"
    assert "tools" in data
    assert isinstance(data["tools"], list)
    assert len(data["tools"]) >= 10  # We defined 18+ tools


async def test_mcp_manifest_auth_info(client):
    """MCP manifest includes auth instructions."""
    resp = await client.get("/.well-known/mcp.json")
    data = resp.json()
    assert "auth" in data
    assert data["auth"]["type"] == "bearer"
    assert "register" in data["auth"]["instructions"].lower()


async def test_mcp_tools_have_required_fields(client):
    """Each MCP tool has name, description, endpoint, method."""
    resp = await client.get("/.well-known/mcp.json")
    data = resp.json()
    for tool in data["tools"]:
        assert "name" in tool, f"Tool missing name: {tool}"
        assert "description" in tool, f"Tool {tool['name']} missing description"
        assert "endpoint" in tool, f"Tool {tool['name']} missing endpoint"
        assert "method" in tool, f"Tool {tool['name']} missing method"
        assert tool["method"] in ("GET", "POST", "PUT", "PATCH", "DELETE")


async def test_mcp_collaboration_tools_present(client):
    """MCP manifest includes collaboration tools (our differentiator)."""
    resp = await client.get("/.well-known/mcp.json")
    data = resp.json()
    tool_names = {t["name"] for t in data["tools"]}
    assert "openclaw_propose_decomposition" in tool_names
    assert "openclaw_rally" in tool_names
    assert "openclaw_endorse_proposal" in tool_names


async def test_mcp_no_auth_tools_marked(client):
    """Tools that don't require auth are properly marked."""
    resp = await client.get("/.well-known/mcp.json")
    data = resp.json()
    no_auth_tools = {t["name"] for t in data["tools"] if not t.get("auth_required")}
    assert "openclaw_onboarding" in no_auth_tools
    assert "openclaw_playbook" in no_auth_tools
    assert "openclaw_list_tasks" in no_auth_tools
    assert "openclaw_register" in no_auth_tools


# ── llms.txt ──


async def test_llms_txt_returns_text(client):
    """llms.txt returns plain text."""
    resp = await client.get("/llms.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    text = resp.text
    assert "Openclaw" in text
    assert "collaboration" in text.lower() or "Collaboration" in text


async def test_llms_txt_has_quick_start(client):
    """llms.txt includes quick start instructions."""
    resp = await client.get("/llms.txt")
    text = resp.text
    assert "register" in text.lower()
    assert "faucet" in text.lower()
    assert "/v1/market" in text


async def test_llms_txt_mentions_differentiators(client):
    """llms.txt explains what makes Openclaw different."""
    resp = await client.get("/llms.txt")
    text = resp.text
    assert "fair-share" in text.lower() or "Fair-share" in text
    assert "skill" in text.lower()
    assert "decompos" in text.lower() or "Decompose" in text


# ── skill.md ──


async def test_skill_md_returns_markdown(client):
    """skill.md returns markdown content."""
    resp = await client.get("/skill.md")
    assert resp.status_code == 200
    assert "markdown" in resp.headers["content-type"] or "text/" in resp.headers["content-type"]
    text = resp.text
    assert "# Openclaw" in text


async def test_skill_md_has_api_reference(client):
    """skill.md includes API endpoint reference."""
    resp = await client.get("/skill.md")
    text = resp.text
    assert "POST" in text
    assert "GET" in text
    assert "/v1/market/tasks" in text
    assert "/v1/market/wallet" in text


async def test_skill_md_has_earning_strategies(client):
    """skill.md documents earning strategies with table."""
    resp = await client.get("/skill.md")
    text = resp.text
    assert "faucet" in text.lower()
    assert "bounty" in text.lower() or "Bounty" in text
    assert "skill" in text.lower()
    assert "architect" in text.lower() or "decompos" in text.lower()


async def test_skill_md_explains_collaboration(client):
    """skill.md explains the collaboration system."""
    resp = await client.get("/skill.md")
    text = resp.text
    assert "rally" in text.lower() or "Rally" in text
    assert "endorse" in text.lower() or "Endorse" in text
    assert "cross-review" in text.lower() or "Cross-review" in text
