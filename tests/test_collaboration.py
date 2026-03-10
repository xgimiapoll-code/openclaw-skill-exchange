"""Tests for task decomposition, rally mechanism, referral, and collective release.

Full lifecycle: decompose → claim subtasks → rally for stuck → complete all → collective release.
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
from app.db import init_db  # noqa: E402

state: dict = {}
BASE = "/v1/market"


@pytest_asyncio.fixture(scope="session")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# ── Setup: Register 4 agents ──


async def test_setup_agents(client):
    """Register boss + 3 workers and give them funds."""
    for name, node in [
        ("Boss", "collab-boss"),
        ("Worker A", "collab-worker-a"),
        ("Worker B", "collab-worker-b"),
        ("Worker C", "collab-worker-c"),
    ]:
        resp = await client.post(f"{BASE}/agents/register", json={
            "node_id": node,
            "display_name": name,
            "skill_tags": ["ml", "data"] if "Worker" in name else ["management"],
        })
        assert resp.status_code == 201
        data = resp.json()
        key = node.replace("collab-", "")
        state[f"{key}_key"] = data["api_key"]
        state[f"{key}_id"] = data["agent"]["agent_id"]

        # Faucet for extra funds
        resp = await client.post(f"{BASE}/wallet/claim-faucet", headers=auth(data["api_key"]))
        assert resp.status_code == 200


# ── Boss creates a parent task ──


async def test_create_parent_task(client):
    """Boss creates a large task worth 50 SHL."""
    resp = await client.post(f"{BASE}/tasks", headers=auth(state["boss_key"]), json={
        "title": "Build ML Pipeline",
        "description": "End-to-end ML pipeline: data collection, training, deployment",
        "bounty_shl": 50,
        "tags": ["ml", "pipeline"],
        "difficulty": "expert",
    })
    assert resp.status_code == 201
    state["parent_id"] = resp.json()["task_id"]


# ── Decompose into subtasks ──


async def test_decompose_task(client):
    """Boss decomposes the task into 3 subtasks with weighted bounties."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/decompose",
        headers=auth(state["boss_key"]),
        json={
            "subtasks": [
                {
                    "title": "Data Collection",
                    "description": "Collect and clean training data",
                    "weight_pct": 25,
                    "tags": ["data"],
                    "difficulty": "easy",
                    "sequence_order": 0,
                },
                {
                    "title": "Model Training",
                    "description": "Train and evaluate the ML model",
                    "weight_pct": 50,
                    "tags": ["ml", "training"],
                    "difficulty": "expert",
                    "sequence_order": 1,
                },
                {
                    "title": "Deployment",
                    "description": "Deploy model to production",
                    "weight_pct": 25,
                    "tags": ["devops"],
                    "difficulty": "medium",
                    "sequence_order": 2,
                },
            ]
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["subtasks"]) == 3
    state["subtask_data"] = data["subtasks"]
    state["subtask_ids"] = [s["task_id"] for s in data["subtasks"]]

    # Verify bounty distribution: 25% of 50 = 12.5, 50% = 25, 25% = 12.5
    assert data["subtasks"][0]["bounty_shl"] == 12.5
    assert data["subtasks"][1]["bounty_shl"] == 25.0
    assert data["subtasks"][2]["bounty_shl"] == 12.5


async def test_decompose_already_decomposed(client):
    """Cannot decompose a task that's already decomposed."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/decompose",
        headers=auth(state["boss_key"]),
        json={"subtasks": [{"title": "X", "description": "Y", "weight_pct": 100}]},
    )
    assert resp.status_code == 400
    assert "already decomposed" in resp.json()["detail"].lower()


async def test_decompose_weight_exceeds_100(client):
    """Cannot decompose with weights > 100%."""
    # Create another task first
    resp = await client.post(f"{BASE}/tasks", headers=auth(state["boss_key"]), json={
        "title": "Another Task",
        "description": "For testing weight validation",
        "bounty_shl": 10,
    })
    assert resp.status_code == 201
    task_id = resp.json()["task_id"]

    resp = await client.post(
        f"{BASE}/tasks/{task_id}/decompose",
        headers=auth(state["boss_key"]),
        json={"subtasks": [
            {"title": "A", "description": "A", "weight_pct": 60},
            {"title": "B", "description": "B", "weight_pct": 60},
        ]},
    )
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["detail"].lower()


# ── List subtasks ──


async def test_list_subtasks(client):
    """List subtasks with stats."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/subtasks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["completed"] == 0
    assert data["all_complete"] is False


# ── Worker A claims and completes the easy subtask (Data Collection) ──


async def test_worker_a_claims_easy_subtask(client):
    """Worker A claims the easy Data Collection subtask."""
    easy_id = state["subtask_ids"][0]
    resp = await client.post(
        f"{BASE}/tasks/{easy_id}/claim",
        headers=auth(state["worker-a_key"]),
    )
    assert resp.status_code == 200
    state["worker_a_claim"] = resp.json()["claim_id"]


async def test_worker_a_submits_easy(client):
    """Worker A submits solution for Data Collection."""
    easy_id = state["subtask_ids"][0]
    resp = await client.post(
        f"{BASE}/tasks/{easy_id}/submissions",
        headers=auth(state["worker-a_key"]),
        json={
            "summary": "Collected 10K labeled images",
            "skill_recipe": {},
            "confidence_score": 0.95,
        },
    )
    assert resp.status_code == 201
    state["easy_submission_id"] = resp.json()["submission_id"]


async def test_boss_completes_easy_subtask(client):
    """Boss selects winner for easy subtask. Reward should NOT release yet."""
    easy_id = state["subtask_ids"][0]
    resp = await client.post(
        f"{BASE}/tasks/{easy_id}/select-winner",
        headers=auth(state["boss_key"]),
        json={
            "submission_id": state["easy_submission_id"],
            "feedback": "Good data quality",
            "rating": 4,
        },
    )
    assert resp.status_code == 200


# ── Worker B claims and completes Deployment subtask ──


async def test_worker_b_claims_deployment(client):
    """Worker B claims the deployment subtask."""
    deploy_id = state["subtask_ids"][2]
    resp = await client.post(
        f"{BASE}/tasks/{deploy_id}/claim",
        headers=auth(state["worker-b_key"]),
    )
    assert resp.status_code == 200
    state["worker_b_claim"] = resp.json()["claim_id"]


async def test_worker_b_submits_deployment(client):
    """Worker B submits deployment solution."""
    deploy_id = state["subtask_ids"][2]
    resp = await client.post(
        f"{BASE}/tasks/{deploy_id}/submissions",
        headers=auth(state["worker-b_key"]),
        json={
            "summary": "Deployed with Docker + K8s",
            "skill_recipe": {},
            "confidence_score": 0.85,
        },
    )
    assert resp.status_code == 201
    state["deploy_submission_id"] = resp.json()["submission_id"]


async def test_boss_completes_deployment(client):
    """Boss accepts deployment. Still can't release — training subtask remains."""
    deploy_id = state["subtask_ids"][2]
    resp = await client.post(
        f"{BASE}/tasks/{deploy_id}/select-winner",
        headers=auth(state["boss_key"]),
        json={
            "submission_id": state["deploy_submission_id"],
            "feedback": "Solid deployment",
            "rating": 4,
        },
    )
    assert resp.status_code == 200


# ── Check release — should fail (training not done) ──


async def test_check_release_not_ready(client):
    """Checking release when not all subtasks done returns escrow message."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/check-release",
        headers=auth(state["boss_key"]),
    )
    assert resp.status_code == 200
    assert "not all" in resp.json()["message"].lower()


# ── Rally: Worker A and B rally for the stuck Model Training subtask ──


async def test_rally_for_stuck_subtask(client):
    """Worker A rallies for the stuck training subtask by staking 2 SHL."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-a_key"]),
        json={
            "target_subtask_id": training_id,
            "stake_shl": 2,
            "message": "Need an ML expert! This is the hard part!",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["stake_shl"] == 2
    assert data["total_rallies"] == 1
    # Bounty should have increased from 25 to 27
    assert data["new_bounty_shl"] == 27.0


async def test_rally_worker_b(client):
    """Worker B also rallies, staking 1 SHL."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-b_key"]),
        json={
            "target_subtask_id": training_id,
            "stake_shl": 1,
            "message": "Help needed!",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_rallies"] == 2
    assert data["new_bounty_shl"] == 28.0  # 25 + 2 + 1


async def test_rally_duplicate_rejected(client):
    """Cannot rally twice for the same subtask."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-a_key"]),
        json={"target_subtask_id": training_id, "stake_shl": 1},
    )
    assert resp.status_code == 400
    assert "already rallied" in resp.json()["detail"].lower()


async def test_rally_non_participant_rejected(client):
    """Cannot rally if you don't have a claim on a sibling subtask."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-c_key"]),
        json={"target_subtask_id": training_id, "stake_shl": 1},
    )
    assert resp.status_code == 400
    assert "participant" in resp.json()["detail"].lower()


# ── Rally status ──


async def test_rally_status(client):
    """Check rally status shows all ralliers and escalated bounty."""
    training_id = state["subtask_ids"][1]
    resp = await client.get(
        f"{BASE}/tasks/{state['parent_id']}/rally-status/{training_id}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["rally_count"] == 2
    assert data["total_staked_shl"] == 3.0  # 2 + 1
    assert data["bounty_shl"] == 28.0
    assert len(data["rallies"]) == 2


# ── Referral: Worker A refers Worker C for the training subtask ──


async def test_create_referral(client):
    """Worker A refers Worker C for the training task."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{training_id}/refer",
        headers=auth(state["worker-a_key"]),
        json={"referred_agent_id": state["worker-c_id"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["referrer_agent_id"] == state["worker-a_id"]
    assert data["referred_agent_id"] == state["worker-c_id"]
    assert data["status"] == "pending"


async def test_self_referral_rejected(client):
    """Cannot refer yourself."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{training_id}/refer",
        headers=auth(state["worker-a_key"]),
        json={"referred_agent_id": state["worker-a_id"]},
    )
    assert resp.status_code == 400
    assert "yourself" in resp.json()["detail"].lower()


# ── Worker C claims and completes the hard training subtask ──


async def test_worker_c_claims_training(client):
    """Worker C (referred) claims the hard training subtask."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{training_id}/claim",
        headers=auth(state["worker-c_key"]),
    )
    assert resp.status_code == 200


async def test_worker_c_submits_training(client):
    """Worker C submits training solution."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{training_id}/submissions",
        headers=auth(state["worker-c_key"]),
        json={
            "summary": "Trained ResNet-50 with 98% accuracy",
            "skill_recipe": {
                "metadata": {"name": "resnet-training", "title": "ResNet Training"},
                "steps": [
                    {"step": 1, "title": "Load data", "action": "load"},
                    {"step": 2, "title": "Train model", "action": "train"},
                ],
            },
            "confidence_score": 0.98,
        },
    )
    assert resp.status_code == 201
    state["training_submission_id"] = resp.json()["submission_id"]


# ── Record wallets before final completion ──


async def test_record_balances_before(client):
    """Record all wallet balances before the final subtask completes."""
    for key in ["worker-a", "worker-b", "worker-c"]:
        resp = await client.get(f"{BASE}/wallet", headers=auth(state[f"{key}_key"]))
        assert resp.status_code == 200
        state[f"{key}_balance_before"] = resp.json()["balance_shl"]


# ── Boss completes the last subtask → triggers collective release ──


async def test_boss_completes_training_triggers_release(client):
    """Boss accepts training solution. This is the last subtask — collective release triggers."""
    training_id = state["subtask_ids"][1]
    resp = await client.post(
        f"{BASE}/tasks/{training_id}/select-winner",
        headers=auth(state["boss_key"]),
        json={
            "submission_id": state["training_submission_id"],
            "feedback": "Excellent model performance!",
            "rating": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    # Should have parent_release info since this was the last subtask
    # (The release happens inside submission_service.complete_task_with_winner)


# ── Verify collective release results ──


async def test_parent_task_completed(client):
    """Parent task should now be completed."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_all_subtasks_completed(client):
    """All subtasks should be completed."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/subtasks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["all_complete"] is True
    assert data["completed"] == 3


async def test_workers_received_bounties(client):
    """All workers should have received their bounties after collective release."""
    for key in ["worker-a", "worker-b", "worker-c"]:
        resp = await client.get(f"{BASE}/wallet", headers=auth(state[f"{key}_key"]))
        assert resp.status_code == 200
        after = resp.json()["balance_shl"]
        before = state[f"{key}_balance_before"]
        # Each worker should have earned something (bounty + bonus - claim deposit returned)
        assert after > before, f"{key} balance didn't increase: {before} -> {after}"


async def test_rally_stakes_returned(client):
    """Rally participants should have gotten their stakes back + bonus."""
    # Worker A staked 2 SHL, Worker B staked 1 SHL
    # Both should get stakes back + 20% bonus
    # We verify by checking the rally status shows 'rewarded'
    training_id = state["subtask_ids"][1]
    resp = await client.get(
        f"{BASE}/tasks/{state['parent_id']}/rally-status/{training_id}"
    )
    assert resp.status_code == 200
    data = resp.json()
    for rally in data["rallies"]:
        assert rally["status"] == "rewarded"


# ── Subtask overview shows final state ──


async def test_final_subtask_overview(client):
    """Final overview shows all subtasks completed with rally data."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/subtasks")
    assert resp.status_code == 200
    data = resp.json()

    # Training subtask (index 1) should show rally activity
    training = next(s for s in data["subtasks"] if s["task_id"] == state["subtask_ids"][1])
    assert training["bounty_shl"] == 28.0  # Escalated by rallies
    assert training["status"] == "completed"
