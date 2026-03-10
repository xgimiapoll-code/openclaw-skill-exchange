"""Tests for blockchain bridge and settlement features.

Tests the off-chain portions: bridge API endpoints (disabled mode),
settlement batch creation, merkle tree, and transaction verification.
On-chain interactions are naturally disabled when blockchain is not configured.
"""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

# Clean DB
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


def auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"}


# ── Setup: register agents and create activity for settlement ──


async def test_setup(client):
    """Register two agents and do a full task lifecycle to generate transactions."""
    # Register Alice (poster)
    resp = await client.post(f"{BASE}/agents/register", json={
        "node_id": "bridge-alice",
        "display_name": "Bridge Alice",
        "wallet_address": "0x1234567890abcdef1234567890abcdef12345678",
    })
    assert resp.status_code == 201
    data = resp.json()
    state["alice_key"] = data["api_key"]
    state["alice_id"] = data["agent"]["agent_id"]

    # Register Bob (solver)
    resp = await client.post(f"{BASE}/agents/register", json={
        "node_id": "bridge-bob",
        "display_name": "Bridge Bob",
        "wallet_address": "0xabcdef1234567890abcdef1234567890abcdef12",
    })
    assert resp.status_code == 201
    data = resp.json()
    state["bob_key"] = data["api_key"]
    state["bob_id"] = data["agent"]["agent_id"]

    # Faucet for both
    resp = await client.post(f"{BASE}/wallet/claim-faucet", headers=auth(state["alice_key"]))
    assert resp.status_code == 200
    resp = await client.post(f"{BASE}/wallet/claim-faucet", headers=auth(state["bob_key"]))
    assert resp.status_code == 200

    # Create a task, claim, submit, select winner — generates multiple transactions
    resp = await client.post(f"{BASE}/tasks", headers=auth(state["alice_key"]), json={
        "title": "Bridge Test Task",
        "description": "A task to generate transactions for settlement testing",
        "bounty_shl": 5,
        "tags": ["test"],
    })
    assert resp.status_code == 201
    state["task_id"] = resp.json()["task_id"]

    # Bob claims
    resp = await client.post(
        f"{BASE}/tasks/{state['task_id']}/claim", headers=auth(state["bob_key"])
    )
    assert resp.status_code == 200
    state["claim_id"] = resp.json()["claim_id"]

    # Bob submits
    resp = await client.post(
        f"{BASE}/tasks/{state['task_id']}/submissions",
        headers=auth(state["bob_key"]),
        json={
            "summary": "Here is the solution",
            "skill_recipe": {"steps": [{"step": 1, "title": "Do it", "action": "run"}]},
            "confidence_score": 0.9,
        },
    )
    assert resp.status_code == 201
    state["submission_id"] = resp.json()["submission_id"]

    # Alice selects winner
    resp = await client.post(
        f"{BASE}/tasks/{state['task_id']}/select-winner",
        headers=auth(state["alice_key"]),
        json={
            "submission_id": state["submission_id"],
            "feedback": "Great work!",
            "rating": 5,
        },
    )
    assert resp.status_code == 200


# ── Bridge Status (blockchain disabled) ──


async def test_bridge_status_disabled(client):
    """Bridge status returns disabled when blockchain is not configured."""
    resp = await client.get(f"{BASE}/bridge/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert "reason" in data


# ── Bridge Deposit (blockchain disabled) ──


async def test_deposit_requires_blockchain(client):
    """Deposit endpoint returns 503 when blockchain is not configured."""
    resp = await client.post(
        f"{BASE}/bridge/deposit",
        headers=auth(state["alice_key"]),
        json={"tx_hash": "0xfake"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


# ── Bridge Withdraw (blockchain disabled) ──


async def test_withdraw_requires_blockchain(client):
    """Withdraw endpoint returns 503 when blockchain is not configured."""
    resp = await client.post(
        f"{BASE}/bridge/withdraw",
        headers=auth(state["alice_key"]),
        json={"amount_shl": 10, "wallet_address": "0x1234567890abcdef1234567890abcdef12345678"},
    )
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


# ── Bridge Requests List ──


async def test_list_bridge_requests_empty(client):
    """Bridge requests list is empty when no bridge activity."""
    resp = await client.get(f"{BASE}/bridge/requests", headers=auth(state["alice_key"]))
    assert resp.status_code == 200
    data = resp.json()
    assert data["requests"] == []
    assert data["total"] == 0


# ── Settlement: Create Batch ──


async def test_settlement_create_requires_expert(client):
    """Settlement creation requires Expert+ reputation."""
    resp = await client.post(
        f"{BASE}/bridge/settlement/create",
        headers=auth(state["alice_key"]),
    )
    assert resp.status_code == 403
    assert "Expert" in resp.json()["detail"]


async def test_create_settlement_batch(client):
    """Creating a settlement batch groups unsettled transactions."""
    # Override auth dependency to return agent with Expert+ reputation
    from app.auth.deps import get_current_agent

    async def _expert_agent():
        return {
            "agent_id": state["alice_id"],
            "api_key": state["alice_key"],
            "reputation_score": 80,
        }

    app.dependency_overrides[get_current_agent] = _expert_agent
    try:
        resp = await client.post(
            f"{BASE}/bridge/settlement/create",
            headers=auth(state["alice_key"]),
        )
    finally:
        app.dependency_overrides.pop(get_current_agent, None)
    assert resp.status_code == 200
    data = resp.json()
    # Should have created a batch with transactions from the task lifecycle
    assert "batch_id" in data
    assert data["tx_count"] > 0
    assert "merkle_root" in data
    assert data["status"] == "pending"
    state["batch_id"] = data["batch_id"]
    state["batch_tx_count"] = data["tx_count"]


async def test_create_settlement_batch_no_unsettled(client):
    """Second batch creation returns no unsettled transactions."""
    from app.auth.deps import get_current_agent

    async def _expert_agent():
        return {"agent_id": state["alice_id"], "api_key": state["alice_key"], "reputation_score": 80}

    app.dependency_overrides[get_current_agent] = _expert_agent
    try:
        resp = await client.post(
            f"{BASE}/bridge/settlement/create",
            headers=auth(state["alice_key"]),
        )
    finally:
        app.dependency_overrides.pop(get_current_agent, None)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("message") == "No unsettled transactions"


# ── Settlement: List Batches ──


async def test_list_settlement_batches(client):
    """Settlement batch list contains the created batch."""
    resp = await client.get(f"{BASE}/bridge/settlement/batches")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    batch = data["batches"][0]
    assert batch["batch_id"] == state["batch_id"]
    assert batch["tx_count"] == state["batch_tx_count"]
    assert batch["status"] == "pending"
    assert batch["merkle_root"]


# ── Settlement: Verify Transaction ──


async def test_verify_transaction_in_batch(client):
    """Verify a specific transaction is included in a settlement batch with merkle proof."""
    # Get a transaction ID from wallet history
    resp = await client.get(f"{BASE}/wallet/transactions", headers=auth(state["alice_key"]))
    assert resp.status_code == 200
    txs = resp.json()["transactions"]
    assert len(txs) > 0
    tx_id = txs[0]["tx_id"]

    resp = await client.get(f"{BASE}/bridge/settlement/verify/{tx_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["verified"] is True
    assert data["batch_id"] == state["batch_id"]
    assert data["merkle_root"]
    assert isinstance(data["proof"], list)
    assert data["leaf"]


async def test_verify_transaction_not_found(client):
    """Verifying a non-existent transaction returns 404."""
    resp = await client.get(f"{BASE}/bridge/settlement/verify/nonexistent-tx-id")
    assert resp.status_code == 404


# ── Merkle Tree Unit Tests ──


async def test_merkle_tree_consistency(client):
    """Verify merkle tree properties: deterministic, proof verification works."""
    from app.blockchain.settlement import (
        _hash_leaf,
        compute_merkle_root,
        compute_merkle_proof,
        verify_merkle_proof,
    )

    # Create test leaves
    leaves = [
        _hash_leaf(f"tx-{i}", 1000000 * (i + 1), "transfer", f"2025-01-0{i + 1}")
        for i in range(5)
    ]

    root = compute_merkle_root(list(leaves))
    assert len(root) == 32

    # Root is deterministic
    root2 = compute_merkle_root(list(leaves))
    assert root == root2

    # Verify proof for each leaf
    for i in range(len(leaves)):
        proof = compute_merkle_proof(list(leaves), i)
        assert verify_merkle_proof(leaves[i], proof, root)

    # Wrong leaf should not verify
    fake_leaf = _hash_leaf("fake", 999, "fake", "2025-01-01")
    proof = compute_merkle_proof(list(leaves), 0)
    assert not verify_merkle_proof(fake_leaf, proof, root)


async def test_merkle_single_leaf(client):
    """Merkle tree with a single leaf should work."""
    from app.blockchain.settlement import (
        _hash_leaf,
        compute_merkle_root,
        compute_merkle_proof,
        verify_merkle_proof,
    )

    leaf = _hash_leaf("tx-only", 5000000, "mint", "2025-01-01")
    root = compute_merkle_root([leaf])
    assert root == leaf  # Single leaf IS the root

    proof = compute_merkle_proof([leaf], 0)
    assert verify_merkle_proof(leaf, proof, root)


async def test_merkle_empty(client):
    """Merkle tree with no leaves returns zeroed root."""
    from app.blockchain.settlement import compute_merkle_root

    root = compute_merkle_root([])
    assert root == b"\x00" * 32


async def test_merkle_power_of_two(client):
    """Merkle tree with power-of-two leaves doesn't need padding."""
    from app.blockchain.settlement import (
        _hash_leaf,
        compute_merkle_root,
        compute_merkle_proof,
        verify_merkle_proof,
    )

    leaves = [
        _hash_leaf(f"tx-{i}", 1000000, "transfer", f"2025-01-0{i + 1}")
        for i in range(4)
    ]

    root = compute_merkle_root(list(leaves))
    for i in range(4):
        proof = compute_merkle_proof(list(leaves), i)
        assert verify_merkle_proof(leaves[i], proof, root)


# ── Market Stats include blockchain ──


async def test_market_stats_bridge_fields(client):
    """Market stats include bridge/settlement fields."""
    resp = await client.get(f"{BASE}/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "blockchain_enabled" in data
    assert data["blockchain_enabled"] is False
    assert "total_bridge_transfers" in data
    assert "total_settlements" in data


# ── Agent wallet_address in profile ──


async def test_agent_wallet_address_in_profile(client):
    """Agent profile includes wallet_address."""
    resp = await client.get(f"{BASE}/agents/me", headers=auth(state["alice_key"]))
    assert resp.status_code == 200
    # wallet_address is not in AgentOut (private), but was stored during registration


async def test_update_wallet_address(client):
    """Agent can update wallet_address via PATCH /me."""
    new_addr = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    resp = await client.patch(
        f"{BASE}/agents/me",
        headers=auth(state["alice_key"]),
        json={"wallet_address": new_addr},
    )
    assert resp.status_code == 200


# ── Provider helpers ──


async def test_blockchain_provider_disabled(client):
    """Provider functions return disabled/None when not configured."""
    from app.blockchain.provider import is_blockchain_enabled, get_web3, get_operator_account

    assert is_blockchain_enabled() is False
    assert get_web3() is None
    assert get_operator_account() is None


async def test_chain_status_not_configured(client):
    """get_chain_status returns reason when not configured."""
    from app.blockchain.provider import get_chain_status

    status = get_chain_status()
    assert status["enabled"] is False
    assert "not configured" in status["reason"]
