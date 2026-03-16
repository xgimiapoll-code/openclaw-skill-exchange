"""Tests for auto-review: webhook notification, AI committee scoring, timeout auto-approve."""

import os
import sys

import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from app.main import app  # noqa: E402
from app.db import SCHEMA_SQL  # noqa: E402

import aiosqlite  # noqa: E402

# Canonical DB path — test_websocket.py reloads app.db with a different path,
# so we must explicitly reset to the correct path.
_CANONICAL_DB = os.path.join(PROJECT_ROOT, "data", "market.db")

state: dict = {}


def _fix_db_path():
    """Ensure ALL references to app.db._DB_PATH point to the canonical test DB.

    Multiple test files (test_background, test_websocket, etc.) reload app.db
    with different DB paths. The routes' get_db may reference any of these
    reloaded modules. We must patch _DB_PATH in ALL module dicts that contain it.
    """
    import app.db as db_mod
    db_mod._DB_PATH = _CANONICAL_DB

    # Collect all unique __globals__ dicts that contain _DB_PATH
    patched: set[int] = set()

    def _patch_fn(fn):
        if fn is None:
            return
        # Unwrap @asynccontextmanager / @contextmanager decorators
        target = getattr(fn, "__wrapped__", fn)
        g = getattr(target, "__globals__", None)
        if g and id(g) not in patched and "_DB_PATH" in g:
            g["_DB_PATH"] = _CANONICAL_DB
            patched.add(id(g))

    # Patch functions on current app.db module
    for fn_name in ("get_db_ctx", "get_db"):
        _patch_fn(getattr(db_mod, fn_name, None))

    # Patch functions imported by ALL modules that reference app.db functions
    # (they may reference a different app.db module object than sys.modules["app.db"])
    import app.routers.agents, app.routers.tasks, app.routers.submissions
    import app.routers.wallet, app.routers.skills, app.routers.disputes
    import app.routers.reputation, app.routers.collaboration, app.routers.guide
    import app.routers.bridge, app.routers.ws
    import app.background.tasks as bg_tasks
    import app.services.webhook_service as wh_svc
    all_mods = [
        app.routers.agents, app.routers.tasks, app.routers.submissions,
        app.routers.wallet, app.routers.skills, app.routers.disputes,
        app.routers.reputation, app.routers.collaboration, app.routers.guide,
        app.routers.bridge, app.routers.ws,
        bg_tasks, wh_svc,
    ]
    for mod in all_mods:
        for attr in ("get_db", "get_db_ctx"):
            _patch_fn(getattr(mod, attr, None))


@pytest_asyncio.fixture(scope="module")
async def client():
    _fix_db_path()

    # Wipe and recreate DB with latest schema (webhook_url, review_method)
    db = await aiosqlite.connect(_CANONICAL_DB)
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = [row[0] for row in await cur.fetchall()]
    await db.execute("PRAGMA foreign_keys=OFF")
    for t in tables:
        await db.execute(f"DROP TABLE IF EXISTS [{t}]")
    await db.commit()
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(SCHEMA_SQL)
    await db.commit()
    await db.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_register_poster(client):
    resp = await client.post(
        "/v1/market/agents/register",
        json={
            "node_id": "ar-poster",
            "display_name": "AR Poster",
            "webhook_url": "https://example.com/webhook",
            "skill_tags": ["python"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    state["poster_key"] = data["api_key"]
    state["poster_id"] = data["agent"]["agent_id"]
    assert data["agent"]["webhook_url"] == "https://example.com/webhook"


async def test_register_solver(client):
    resp = await client.post(
        "/v1/market/agents/register",
        json={
            "node_id": "ar-solver",
            "display_name": "AR Solver",
            "skill_tags": ["python", "scraping"],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    state["solver_key"] = data["api_key"]
    state["solver_id"] = data["agent"]["agent_id"]


async def test_post_task(client):
    resp = await client.post(
        "/v1/market/tasks",
        json={
            "title": "Auto-review test task",
            "description": "Task to test auto-review mechanisms",
            "bounty_shl": 20,
            "tags": ["python"],
            "difficulty": "easy",
            "deadline_hours": 1,
        },
        headers={"Authorization": f"Bearer {state['poster_key']}"},
    )
    assert resp.status_code == 201
    state["task_id"] = resp.json()["task_id"]


async def test_claim_task(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/claim",
        headers={"Authorization": f"Bearer {state['solver_key']}"},
    )
    assert resp.status_code == 200


async def test_submit_solution(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['task_id']}/submissions",
        json={
            "summary": "Built a comprehensive web scraper with multi-site support. Features: 1. Adapter pattern for different sites. 2. Robust error handling. 3. Rate limiting built-in.",
            "confidence_score": 0.85,
        },
        headers={"Authorization": f"Bearer {state['solver_key']}"},
    )
    assert resp.status_code == 201
    state["submission_id"] = resp.json()["submission_id"]


async def test_task_in_review(client):
    resp = await client.get(f"/v1/market/tasks/{state['task_id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_review"


# ── AI Committee Scoring ──


async def test_ai_committee_scores_submission(client):
    """AI committee should score the submission after grace period."""
    import app.services.auto_review as ar_mod

    _fix_db_path()
    orig = ar_mod.config.auto_review_grace_hours
    ar_mod.config.auto_review_grace_hours = 0
    try:
        await ar_mod.ai_committee_review()
    finally:
        ar_mod.config.auto_review_grace_hours = orig

    # Check that submission now has AI feedback
    resp = await client.get(f"/v1/market/tasks/{state['task_id']}/submissions")
    assert resp.status_code == 200
    submissions = resp.json()
    assert len(submissions) == 1
    assert submissions[0]["poster_feedback"] is not None
    assert "[AI Committee]" in submissions[0]["poster_feedback"]
    assert "Score:" in submissions[0]["poster_feedback"]


# ── Timeout Auto-Approve ──


async def test_auto_approve_selects_winner(client):
    """Auto-approve should select best submission after timeout."""
    import app.services.auto_review as ar_mod
    from app.services.auto_review import auto_approve_stale_reviews

    _fix_db_path()
    orig = ar_mod.config.auto_approve_timeout_hours
    ar_mod.config.auto_approve_timeout_hours = 0
    try:
        await auto_approve_stale_reviews()
    finally:
        ar_mod.config.auto_approve_timeout_hours = orig

    # Task should be completed
    resp = await client.get(f"/v1/market/tasks/{state['task_id']}")
    assert resp.status_code == 200
    task = resp.json()
    assert task["status"] == "completed"
    assert task["winning_submission_id"] == state["submission_id"]
    assert task["review_method"] == "ai_committee"  # AI scored before auto-approve


async def test_solver_received_bounty(client):
    """Solver should receive bounty after auto-approve."""
    resp = await client.get(
        "/v1/market/wallet",
        headers={"Authorization": f"Bearer {state['solver_key']}"},
    )
    assert resp.status_code == 200
    wallet = resp.json()
    # Started with 100, claimed deposit -1, refunded +1, bounty +20, bonus +2
    assert wallet["balance_shl"] >= 120.0


# ── AI Committee Scoring Function ──


async def test_score_submission_quality():
    """Test that score_submission produces reasonable scores."""
    from app.services.auto_review import score_submission

    high = score_submission(
        {"summary": "Built a comprehensive solution. 1. Feature A. 2. Feature B. 3. Implemented tests. Handles edge cases.", "confidence_score": 0.9, "skill_recipe": "{}"},
        {"reputation_score": 50.0, "total_tasks_solved": 3, "skill_tags": '["python"]'},
        {"tags": '["python"]'},
    )

    low = score_submission(
        {"summary": "done", "confidence_score": 0.3, "skill_recipe": "{}"},
        {"reputation_score": 0.0, "total_tasks_solved": 0, "skill_tags": "[]"},
        {"tags": '["python"]'},
    )

    assert high > low
    assert high > 30
    assert low < 30


# ── Webhook URL in Agent Profile ──


async def test_update_webhook_url(client):
    """Agent can update webhook URL."""
    resp = await client.patch(
        "/v1/market/agents/me",
        json={"webhook_url": "https://new-webhook.example.com/events"},
        headers={"Authorization": f"Bearer {state['poster_key']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["webhook_url"] == "https://new-webhook.example.com/events"


# ── Poster Manual Review Records Method ──


async def test_poster_manual_review_records_method(client):
    """When poster manually selects winner, review_method = 'poster'."""
    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "manual-poster", "display_name": "Manual Poster"},
    )
    mp_key = resp.json()["api_key"]

    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "manual-solver", "display_name": "Manual Solver"},
    )
    ms_key = resp.json()["api_key"]

    resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Manual review test", "description": "Test", "bounty_shl": 5},
        headers={"Authorization": f"Bearer {mp_key}"},
    )
    tid = resp.json()["task_id"]

    await client.post(f"/v1/market/tasks/{tid}/claim", headers={"Authorization": f"Bearer {ms_key}"})
    resp = await client.post(
        f"/v1/market/tasks/{tid}/submissions",
        json={"summary": "Solution here", "confidence_score": 0.8},
        headers={"Authorization": f"Bearer {ms_key}"},
    )
    sid = resp.json()["submission_id"]

    resp = await client.post(
        f"/v1/market/tasks/{tid}/select-winner",
        json={"submission_id": sid, "rating": 4, "feedback": "Good work!"},
        headers={"Authorization": f"Bearer {mp_key}"},
    )
    assert resp.status_code == 200

    resp = await client.get(f"/v1/market/tasks/{tid}")
    assert resp.json()["review_method"] == "poster"
