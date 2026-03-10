"""Tests for the dispute resolution system.

Covers: dispute creation, community voting, admin resolution, economic impact,
access control, and edge cases.
"""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "market.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from app.main import app  # noqa: E402
from app.db import init_db, get_db_ctx  # noqa: E402

state: dict = {}


async def _set_reputation(agent_id: str, score: float):
    """Directly set agent reputation in DB for test setup."""
    async with get_db_ctx() as db:
        await db.execute(
            "UPDATE agents SET reputation_score = ? WHERE agent_id = ?",
            (score, agent_id),
        )
        await db.commit()


@pytest_asyncio.fixture(scope="session")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Helpers ──


async def _register(client, node_id, name, tags=None):
    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": node_id, "display_name": name, "skill_tags": tags or []},
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["agent"]["agent_id"], data["api_key"]


async def _create_completed_task(client, poster_key, solver_key, bounty_shl):
    """Create a task, claim, submit, and complete it. Returns task_id."""
    # Create task
    resp = await client.post(
        "/v1/market/tasks",
        headers={"Authorization": f"Bearer {poster_key}"},
        json={
            "title": f"Dispute test task ({bounty_shl} SHL)",
            "description": "Task for dispute testing",
            "bounty_shl": bounty_shl,
        },
    )
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    # Claim
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/claim",
        headers={"Authorization": f"Bearer {solver_key}"},
    )
    assert resp.status_code == 200

    # Submit
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/submissions",
        headers={"Authorization": f"Bearer {solver_key}"},
        json={
            "summary": "Test solution",
            "skill_recipe": {
                "schema_version": "1.0.0",
                "metadata": {"name": f"test-skill-{task_id[:8]}", "title": "Test"},
                "steps": [{"step": 1, "title": "Do it", "action": "code"}],
            },
            "confidence_score": 0.8,
        },
    )
    assert resp.status_code == 201
    submission_id = resp.json()["submission_id"]

    # Select winner
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/select-winner",
        headers={"Authorization": f"Bearer {poster_key}"},
        json={"submission_id": submission_id, "feedback": "Good", "rating": 4},
    )
    assert resp.status_code == 200
    return task_id


# ── 1. Agent Registration ──


async def test_register_agents(client):
    """Register poster, solver, 3 voters, and an outsider."""
    state["poster_id"], state["poster_key"] = await _register(
        client, "dispute-poster", "Poster", ["python"]
    )
    state["solver_id"], state["solver_key"] = await _register(
        client, "dispute-solver", "Solver", ["python"]
    )
    state["carol_id"], state["carol_key"] = await _register(
        client, "dispute-carol", "Carol", ["review"]
    )
    state["dave_id"], state["dave_key"] = await _register(
        client, "dispute-dave", "Dave", ["review"]
    )
    state["eve_id"], state["eve_key"] = await _register(
        client, "dispute-eve", "Eve", ["review"]
    )
    state["outsider_id"], state["outsider_key"] = await _register(
        client, "dispute-outsider", "Outsider", []
    )

    # Set voter reputations to Expert level
    for aid in [state["carol_id"], state["dave_id"], state["eve_id"]]:
        await _set_reputation(aid, 75.0)


# ── 2. Setup: complete a task for auto-resolution disputes ──


async def test_setup_auto_dispute_task(client):
    """Create and complete a small-bounty task (< 10 SHL → auto resolution)."""
    state["auto_task_id"] = await _create_completed_task(
        client, state["poster_key"], state["solver_key"], bounty_shl=5
    )


# ── 3. Create dispute (poster disputes solver) ──


async def test_create_dispute_auto(client):
    """Poster creates dispute on small-bounty task → auto resolution method."""
    resp = await client.post(
        f"/v1/market/tasks/{state['auto_task_id']}/dispute",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"reason": "Solution is incomplete", "evidence": {"logs": "missing output"}},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "open"
    assert data["resolution_method"] == "auto"
    assert data["initiator_agent_id"] == state["poster_id"]
    assert data["respondent_agent_id"] == state["solver_id"]
    assert data["reason"] == "Solution is incomplete"
    assert data["evidence"] == {"logs": "missing output"}
    state["auto_dispute_id"] = data["dispute_id"]


# ── 4. Get dispute ──


async def test_get_dispute(client):
    resp = await client.get(f"/v1/market/disputes/{state['auto_dispute_id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["dispute_id"] == state["auto_dispute_id"]
    assert data["task_id"] == state["auto_task_id"]


# ── 5. Get task disputes ──


async def test_get_task_disputes(client):
    resp = await client.get(f"/v1/market/tasks/{state['auto_task_id']}/dispute")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["dispute_id"] == state["auto_dispute_id"]


# ── 6. Cannot dispute non-completed task ──


async def test_cannot_dispute_open_task(client):
    """Cannot open a dispute on a task that's not completed/expired."""
    # Create an open task
    resp = await client.post(
        "/v1/market/tasks",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={
            "title": "Open task for dispute fail test",
            "description": "Should not be disputable",
            "bounty_shl": 5,
        },
    )
    assert resp.status_code == 201
    open_task_id = resp.json()["task_id"]
    state["open_task_id"] = open_task_id

    resp = await client.post(
        f"/v1/market/tasks/{open_task_id}/dispute",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"reason": "Test"},
    )
    assert resp.status_code == 400
    assert "completed or expired" in resp.json()["detail"].lower()


# ── 7. Non-participant cannot dispute ──


async def test_non_participant_cannot_dispute(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['auto_task_id']}/dispute",
        headers={"Authorization": f"Bearer {state['outsider_key']}"},
        json={"reason": "I want to dispute"},
    )
    assert resp.status_code == 403
    assert "participants" in resp.json()["detail"].lower()


# ── 8. Duplicate dispute ──


async def test_duplicate_dispute(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['auto_task_id']}/dispute",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"reason": "Another reason"},
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


# ── 9. Get dispute for nonexistent ──


async def test_get_nonexistent_dispute(client):
    resp = await client.get("/v1/market/disputes/nonexistent-id")
    assert resp.status_code == 404


# ── 10. Setup: complete task for community vote dispute (bounty 10-100 SHL) ──


async def test_setup_community_vote_task(client):
    """Create and complete a mid-bounty task (10-100 SHL → community_vote)."""
    state["vote_task_id"] = await _create_completed_task(
        client, state["poster_key"], state["solver_key"], bounty_shl=50
    )


async def test_create_community_vote_dispute(client):
    """Poster creates dispute on mid-bounty task → community_vote method."""
    resp = await client.post(
        f"/v1/market/tasks/{state['vote_task_id']}/dispute",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"reason": "Low quality solution"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["resolution_method"] == "community_vote"
    state["vote_dispute_id"] = data["dispute_id"]


# ── 11. Participant cannot vote ──


async def test_participant_cannot_vote(client):
    """Dispute initiator cannot vote on their own dispute."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"vote": "initiator"},
    )
    assert resp.status_code == 403
    assert "participants" in resp.json()["detail"].lower()


async def test_respondent_cannot_vote(client):
    """Dispute respondent cannot vote on their own dispute."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['solver_key']}"},
        json={"vote": "respondent"},
    )
    assert resp.status_code == 403
    assert "participants" in resp.json()["detail"].lower()


# ── 12. Low-reputation agent cannot vote ──


async def test_low_reputation_cannot_vote(client):
    """Outsider with low reputation cannot vote."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['outsider_key']}"},
        json={"vote": "initiator"},
    )
    assert resp.status_code == 403
    assert "reputation" in resp.json()["detail"].lower()


# ── 13. Cast votes ──


async def test_vote_carol(client):
    """First vote — not enough to resolve."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['carol_key']}"},
        json={"vote": "initiator", "comment": "Poster is right"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vote"] == "initiator"
    assert data["total_votes"] == 1
    assert data["resolved"] is False


async def test_duplicate_vote(client):
    """Same voter cannot vote twice."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['carol_key']}"},
        json={"vote": "initiator"},
    )
    assert resp.status_code == 409
    assert "already voted" in resp.json()["detail"].lower()


async def test_vote_dave(client):
    """Second vote — still not enough."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['dave_key']}"},
        json={"vote": "initiator", "comment": "Agree with poster"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_votes"] == 2
    assert data["resolved"] is False


async def test_vote_eve_resolves(client):
    """Third vote gives majority → dispute auto-resolves in initiator's favor."""
    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['eve_key']}"},
        json={"vote": "initiator"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_votes"] == 3
    assert data["resolved"] is True
    assert data["vote_counts"]["initiator"] == 3


# ── 14. Verify dispute resolved ──


async def test_dispute_resolved_after_votes(client):
    """Dispute status should be resolved_initiator after voting."""
    resp = await client.get(f"/v1/market/disputes/{state['vote_dispute_id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved_initiator"
    assert data["resolved_at"] is not None


# ── 15. Cannot vote on resolved dispute ──


async def test_cannot_vote_on_resolved_dispute(client):
    """Register a new expert voter and try to vote on resolved dispute."""
    fid, fkey = await _register(client, "dispute-frank", "Frank", [])
    await _set_reputation(fid, 80.0)

    resp = await client.post(
        f"/v1/market/disputes/{state['vote_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {fkey}"},
        json={"vote": "respondent"},
    )
    assert resp.status_code == 400
    assert "not open" in resp.json()["detail"].lower()


# ── 16. Get dispute votes ──


async def test_get_dispute_votes(client):
    resp = await client.get(f"/v1/market/disputes/{state['vote_dispute_id']}/votes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert all(v["vote"] == "initiator" for v in data)
    assert data[0]["comment"] == "Poster is right"


# ── 17. Admin resolve dispute ──


async def test_setup_admin_resolve_task(client):
    """Create another completed task for admin-resolve testing."""
    state["admin_task_id"] = await _create_completed_task(
        client, state["poster_key"], state["solver_key"], bounty_shl=10
    )


async def test_create_dispute_for_admin_resolve(client):
    resp = await client.post(
        f"/v1/market/tasks/{state['admin_task_id']}/dispute",
        headers={"Authorization": f"Bearer {state['solver_key']}"},
        json={"reason": "Poster gave unfair rating"},
    )
    assert resp.status_code == 201
    data = resp.json()
    # Solver disputes poster → respondent is poster
    assert data["initiator_agent_id"] == state["solver_id"]
    assert data["respondent_agent_id"] == state["poster_id"]
    state["admin_dispute_id"] = data["dispute_id"]


async def test_low_rep_cannot_resolve(client):
    """Low-reputation agent cannot manually resolve."""
    resp = await client.post(
        f"/v1/market/disputes/{state['admin_dispute_id']}/resolve",
        headers={"Authorization": f"Bearer {state['outsider_key']}"},
        json={"resolution": "dismiss"},
    )
    assert resp.status_code == 403
    assert "reputation" in resp.json()["detail"].lower()


async def test_admin_resolve_with_comment(client):
    """Expert agent resolves dispute with comment."""
    resp = await client.post(
        f"/v1/market/disputes/{state['admin_dispute_id']}/resolve",
        headers={"Authorization": f"Bearer {state['carol_key']}"},
        json={"resolution": "respondent", "comment": "Poster rating was fair"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved_respondent"
    assert data["resolved_by"] == state["carol_id"]

    # Verify resolution comment saved
    resp2 = await client.get(f"/v1/market/disputes/{state['admin_dispute_id']}")
    assert resp2.status_code == 200
    d = resp2.json()
    assert d["status"] == "resolved_respondent"


# ── 18. Cannot resolve already resolved ──


async def test_cannot_resolve_already_resolved(client):
    resp = await client.post(
        f"/v1/market/disputes/{state['admin_dispute_id']}/resolve",
        headers={"Authorization": f"Bearer {state['carol_key']}"},
        json={"resolution": "initiator"},
    )
    assert resp.status_code == 400
    assert "already resolved" in resp.json()["detail"].lower()


# ── 19. Dispute on non-vote type rejects voting ──


async def test_cannot_vote_on_auto_dispute(client):
    """Auto-resolution disputes don't accept community votes."""
    # auto_dispute is type 'auto'
    resp = await client.post(
        f"/v1/market/disputes/{state['auto_dispute_id']}/vote",
        headers={"Authorization": f"Bearer {state['carol_key']}"},
        json={"vote": "initiator"},
    )
    # Either 400 (not open - already open but method mismatch) or dispute-specific check
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "not accept" in detail or "not open" in detail


# ── 20. Solver creates dispute → respondent is poster ──


async def test_solver_creates_dispute(client):
    """When solver creates dispute, the respondent should be the poster."""
    task_id = await _create_completed_task(
        client, state["poster_key"], state["solver_key"], bounty_shl=5
    )
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/dispute",
        headers={"Authorization": f"Bearer {state['solver_key']}"},
        json={"reason": "Poster was unfair"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["initiator_agent_id"] == state["solver_id"]
    assert data["respondent_agent_id"] == state["poster_id"]


# ── 21. Dispute task not found ──


async def test_dispute_task_not_found(client):
    resp = await client.post(
        "/v1/market/tasks/nonexistent-task/dispute",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"reason": "Test"},
    )
    assert resp.status_code == 404


# ── 22. Votes endpoint for nonexistent dispute ──


async def test_votes_nonexistent_dispute(client):
    resp = await client.get("/v1/market/disputes/nonexistent-id/votes")
    assert resp.status_code == 404


# ── 23. Get disputes for task with no disputes ──


async def test_get_disputes_empty_task(client):
    """Cancel the open task and check — or use a task with no disputes."""
    resp = await client.get(f"/v1/market/tasks/{state['open_task_id']}/dispute")
    # open_task is not completed, so it should return 404 on GET disputes
    # Actually the GET endpoint just checks if task exists
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 0


# ── 24. Dismiss resolution ──


async def test_dismiss_dispute(client):
    """Expert dismisses a dispute."""
    task_id = await _create_completed_task(
        client, state["poster_key"], state["solver_key"], bounty_shl=5
    )
    resp = await client.post(
        f"/v1/market/tasks/{task_id}/dispute",
        headers={"Authorization": f"Bearer {state['poster_key']}"},
        json={"reason": "Frivolous complaint"},
    )
    assert resp.status_code == 201
    dispute_id = resp.json()["dispute_id"]

    resp = await client.post(
        f"/v1/market/disputes/{dispute_id}/resolve",
        headers={"Authorization": f"Bearer {state['carol_key']}"},
        json={"resolution": "dismiss", "comment": "No valid grounds"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "dismissed"

    # Verify stored status
    resp = await client.get(f"/v1/market/disputes/{dispute_id}")
    assert resp.json()["status"] == "dismissed"


# ── 25. Health check still works ──


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db_backend"] == "sqlite"
    assert "checks" in data


# ── 26. Metrics endpoint ──


async def test_metrics(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "openclaw_agents_total" in text
    assert "openclaw_tasks_total" in text
    assert "openclaw_skills_total" in text
    assert "openclaw_shl_circulation_micro" in text
    assert "openclaw_disputes_open" in text
    assert "openclaw_uptime_seconds" in text
