"""Tests for decentralized task decomposition, fair-share distribution, rally, and cross-review.

Full lifecycle:
  propose → endorse → activate → claim subtasks → rally → cross-review →
  complete all → fair-share algorithm computes distribution → collective release
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


# ── Setup: 5 agents (Boss + 3 Workers + 1 Architect) ──


async def test_setup_agents(client):
    """Register agents and give them funds."""
    agents = [
        ("Boss", "collab-boss", ["management"]),
        ("Worker A", "collab-worker-a", ["data", "etl"]),
        ("Worker B", "collab-worker-b", ["devops", "deploy"]),
        ("Worker C", "collab-worker-c", ["ml", "training"]),
        ("Architect", "collab-architect", ["architecture", "ml", "data"]),
    ]
    for name, node, tags in agents:
        resp = await client.post(f"{BASE}/agents/register", json={
            "node_id": node, "display_name": name, "skill_tags": tags,
        })
        assert resp.status_code == 201
        data = resp.json()
        key = node.replace("collab-", "")
        state[f"{key}_key"] = data["api_key"]
        state[f"{key}_id"] = data["agent"]["agent_id"]

        resp = await client.post(f"{BASE}/wallet/claim-faucet", headers=auth(data["api_key"]))
        assert resp.status_code == 200


# ── Boss creates parent task ──


async def test_create_parent_task(client):
    """Boss posts a 50 SHL task."""
    resp = await client.post(f"{BASE}/tasks", headers=auth(state["boss_key"]), json={
        "title": "Build ML Pipeline",
        "description": "End-to-end ML pipeline: data, training, deployment",
        "bounty_shl": 50,
        "tags": ["ml", "pipeline"],
        "difficulty": "expert",
    })
    assert resp.status_code == 201
    state["parent_id"] = resp.json()["task_id"]


# ── Decentralized Decomposition: Architect proposes ──


async def test_architect_proposes_decomposition(client):
    """Architect (not the boss) proposes a task decomposition."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/propose",
        headers=auth(state["architect_key"]),
        json={
            "subtasks": [
                {
                    "title": "Data Collection & ETL",
                    "description": "Collect, clean, and transform training data",
                    "tags": ["data", "etl"],
                    "difficulty": "easy",
                    "sequence_order": 0,
                },
                {
                    "title": "Model Training & Evaluation",
                    "description": "Train ML model, hyperparameter tuning, evaluate accuracy",
                    "tags": ["ml", "training"],
                    "difficulty": "expert",
                    "sequence_order": 1,
                },
                {
                    "title": "Production Deployment",
                    "description": "Deploy trained model with monitoring",
                    "tags": ["devops", "deploy"],
                    "difficulty": "medium",
                    "sequence_order": 2,
                },
            ]
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    state["proposal_id"] = data["proposal_id"]
    assert data["status"] == "proposed"
    assert data["subtask_count"] == 3


async def test_list_proposals(client):
    """List proposals shows the architect's proposal."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["proposals"]) == 1
    assert data["proposals"][0]["proposer_name"] == "Architect"


# ── Boss endorses → immediate activation ──


async def test_boss_endorses_activates(client):
    """Boss (poster) endorsing a proposal activates it immediately."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/proposals/{state['proposal_id']}/endorse",
        headers=auth(state["boss_key"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["activated"] is True


async def test_subtasks_created(client):
    """Subtasks were created with equal initial bounty split."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/subtasks")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    state["subtask_ids"] = [s["task_id"] for s in data["subtasks"]]

    # Equal initial split: 50/3 ≈ 16.666 SHL each
    for s in data["subtasks"]:
        assert s["bounty_shl"] > 0
        assert s["weight_pct"] == 0  # No fixed weight — fair share decides


# ── Boss direct decompose (backward compat) ──


async def test_direct_decompose_still_works(client):
    """Boss can still use direct decompose for a different task."""
    resp = await client.post(f"{BASE}/tasks", headers=auth(state["boss_key"]), json={
        "title": "Simple Task", "description": "Test direct decompose", "bounty_shl": 10,
    })
    assert resp.status_code == 201
    tid = resp.json()["task_id"]

    resp = await client.post(
        f"{BASE}/tasks/{tid}/decompose",
        headers=auth(state["boss_key"]),
        json={"subtasks": [
            {"title": "Part A", "description": "Do A"},
            {"title": "Part B", "description": "Do B"},
        ]},
    )
    assert resp.status_code == 201
    assert "fair-share" in resp.json()["message"].lower()


async def test_cannot_double_decompose(client):
    """Cannot decompose already decomposed task."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/propose",
        headers=auth(state["architect_key"]),
        json={"subtasks": [
            {"title": "X", "description": "X"},
            {"title": "Y", "description": "Y"},
        ]},
    )
    assert resp.status_code == 400


# ── Workers claim and complete subtasks ──


async def test_worker_a_claims_data(client):
    """Worker A claims the easy data subtask."""
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][0]}/claim",
        headers=auth(state["worker-a_key"]),
    )
    assert resp.status_code == 200


async def test_worker_a_submits(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][0]}/submissions",
        headers=auth(state["worker-a_key"]),
        json={"summary": "Collected 10K images", "skill_recipe": {}, "confidence_score": 0.9},
    )
    assert resp.status_code == 201
    state["sub_a_id"] = resp.json()["submission_id"]


async def test_boss_accepts_data(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][0]}/select-winner",
        headers=auth(state["boss_key"]),
        json={"submission_id": state["sub_a_id"], "feedback": "Good data", "rating": 4},
    )
    assert resp.status_code == 200


async def test_worker_b_claims_deploy(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][2]}/claim",
        headers=auth(state["worker-b_key"]),
    )
    assert resp.status_code == 200


async def test_worker_b_submits(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][2]}/submissions",
        headers=auth(state["worker-b_key"]),
        json={"summary": "Deployed with Docker", "skill_recipe": {}, "confidence_score": 0.8},
    )
    assert resp.status_code == 201
    state["sub_b_id"] = resp.json()["submission_id"]


async def test_boss_accepts_deploy(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][2]}/select-winner",
        headers=auth(state["boss_key"]),
        json={"submission_id": state["sub_b_id"], "feedback": "Solid deploy", "rating": 4},
    )
    assert resp.status_code == 200


# ── Rally for stuck training subtask ──


async def test_rally(client):
    """Worker A rallies for stuck training subtask."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-a_key"]),
        json={
            "target_subtask_id": state["subtask_ids"][1],
            "stake_shl": 2,
            "message": "Need an ML expert!",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["total_rallies"] == 1


async def test_rally_worker_b(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-b_key"]),
        json={"target_subtask_id": state["subtask_ids"][1], "stake_shl": 1},
    )
    assert resp.status_code == 200
    assert resp.json()["total_rallies"] == 2


async def test_rally_non_participant_rejected(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/rally",
        headers=auth(state["worker-c_key"]),
        json={"target_subtask_id": state["subtask_ids"][1], "stake_shl": 1},
    )
    assert resp.status_code == 400


# ── Referral ──


async def test_referral(client):
    """Worker A refers Worker C for training subtask."""
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][1]}/refer",
        headers=auth(state["worker-a_key"]),
        json={"referred_agent_id": state["worker-c_id"]},
    )
    assert resp.status_code == 200


# ── Fair Share Preview (before completion) ──


async def test_fair_share_preview(client):
    """Preview fair-share distribution before all complete."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/fair-shares")
    assert resp.status_code == 200
    data = resp.json()
    assert data["algorithm"] == "fair_share_v1"
    assert len(data["shares"]) == 3
    # Training (expert, rallied, unclaimed) should have highest difficulty score
    shares = {s["subtask_id"]: s for s in data["shares"]}
    training_share = shares[state["subtask_ids"][1]]
    data_share = shares[state["subtask_ids"][0]]
    assert training_share["components"]["difficulty"] > data_share["components"]["difficulty"]
    # Total shares should sum to ~100%
    total = sum(s["share_pct"] for s in data["shares"])
    assert 99.0 <= total <= 101.0


# ── Worker C completes training ──


async def test_worker_c_claims_training(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][1]}/claim",
        headers=auth(state["worker-c_key"]),
    )
    assert resp.status_code == 200


async def test_worker_c_submits(client):
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][1]}/submissions",
        headers=auth(state["worker-c_key"]),
        json={
            "summary": "Trained ResNet-50, 98% accuracy",
            "skill_recipe": {
                "metadata": {"name": "resnet-training", "title": "ResNet Training"},
                "steps": [{"step": 1, "title": "Train", "action": "train"}],
            },
            "confidence_score": 0.98,
        },
    )
    assert resp.status_code == 201
    state["sub_c_id"] = resp.json()["submission_id"]


# ── Cross-reviews (before final completion) ──


async def test_cross_review_a_reviews_b(client):
    """Worker A reviews Worker B's deployment work."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/cross-review",
        headers=auth(state["worker-a_key"]),
        json={"reviewed_subtask_id": state["subtask_ids"][2], "score": 4, "comment": "Clean deploy"},
    )
    assert resp.status_code == 200


async def test_cross_review_cannot_review_self(client):
    """Worker A cannot cross-review their own subtask."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/cross-review",
        headers=auth(state["worker-a_key"]),
        json={"reviewed_subtask_id": state["subtask_ids"][0], "score": 5},
    )
    assert resp.status_code == 400


async def test_cross_review_non_solver_rejected(client):
    """Architect (not a solver) cannot cross-review."""
    resp = await client.post(
        f"{BASE}/tasks/{state['parent_id']}/cross-review",
        headers=auth(state["architect_key"]),
        json={"reviewed_subtask_id": state["subtask_ids"][0], "score": 5},
    )
    assert resp.status_code == 400


# ── Record balances before release ──


async def test_record_balances(client):
    for key in ["worker-a", "worker-b", "worker-c"]:
        resp = await client.get(f"{BASE}/wallet", headers=auth(state[f"{key}_key"]))
        assert resp.status_code == 200
        state[f"{key}_balance_before"] = resp.json()["balance_shl"]


# ── Complete last subtask → triggers fair-share release ──


async def test_boss_completes_training(client):
    """Last subtask completion triggers fair-share algorithm and collective release."""
    resp = await client.post(
        f"{BASE}/tasks/{state['subtask_ids'][1]}/select-winner",
        headers=auth(state["boss_key"]),
        json={"submission_id": state["sub_c_id"], "feedback": "Excellent!", "rating": 5},
    )
    assert resp.status_code == 200


# ── Verify fair-share release ──


async def test_parent_completed(client):
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_all_subtasks_completed(client):
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/subtasks")
    assert resp.status_code == 200
    assert resp.json()["all_complete"] is True


async def test_workers_received_bounties(client):
    """All workers got paid. Training solver should get the most (hardest task)."""
    balances = {}
    for key in ["worker-a", "worker-b", "worker-c"]:
        resp = await client.get(f"{BASE}/wallet", headers=auth(state[f"{key}_key"]))
        after = resp.json()["balance_shl"]
        before = state[f"{key}_balance_before"]
        earned = after - before
        balances[key] = earned
        assert earned > 0, f"{key} didn't earn anything"

    # Worker C (expert training, high difficulty) should earn >= Worker A (easy data)
    assert balances["worker-c"] >= balances["worker-a"], \
        f"Training solver ({balances['worker-c']}) should earn >= data solver ({balances['worker-a']})"


async def test_rally_stakes_returned(client):
    resp = await client.get(
        f"{BASE}/tasks/{state['parent_id']}/rally-status/{state['subtask_ids'][1]}"
    )
    assert resp.status_code == 200
    for rally in resp.json()["rallies"]:
        assert rally["status"] == "rewarded"


# ── Fair Share final state ──


async def test_fair_share_final(client):
    """Verify the fair-share algorithm produced sane results."""
    resp = await client.get(f"{BASE}/tasks/{state['parent_id']}/fair-shares")
    assert resp.status_code == 200
    data = resp.json()
    shares = {s["subtask_id"]: s for s in data["shares"]}

    # Training subtask should have highest share (expert difficulty + rallied)
    training = shares[state["subtask_ids"][1]]
    data_task = shares[state["subtask_ids"][0]]

    assert training["share_pct"] > data_task["share_pct"], \
        f"Training share {training['share_pct']}% should > data share {data_task['share_pct']}%"

    # Verify components are populated
    assert "difficulty" in training["components"]
    assert "quality" in training["components"]
    assert "scarcity" in training["components"]
    assert "dependency" in training["components"]
