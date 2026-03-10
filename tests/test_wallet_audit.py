"""Token audit tests — verify ledger integrity."""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "audit_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Override DB path before importing app
os.environ["MARKET_DB_PATH"] = DB_PATH

# Force re-import with new DB path
if "app.config" in sys.modules:
    del sys.modules["app.config"]
if "app.db" in sys.modules:
    del sys.modules["app.db"]

from app.db import init_db, get_db_ctx  # noqa: E402

# Re-import app after config change
import importlib
import app.config
importlib.reload(app.config)
import app.db
importlib.reload(app.db)
from app.main import app  # noqa: E402


@pytest_asyncio.fixture(scope="module")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_full_lifecycle_audit(client):
    """Run a full lifecycle and verify all transactions balance."""
    # Register two agents
    r1 = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "audit-poster", "display_name": "Poster"},
    )
    poster_key = r1.json()["api_key"]
    poster_h = {"Authorization": f"Bearer {poster_key}"}

    r2 = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "audit-solver", "display_name": "Solver"},
    )
    solver_key = r2.json()["api_key"]
    solver_h = {"Authorization": f"Bearer {solver_key}"}

    # Post task with 30 SHL bounty
    task_resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Audit task", "description": "Test", "bounty_shl": 30},
        headers=poster_h,
    )
    task_id = task_resp.json()["task_id"]

    # Claim
    await client.post(f"/v1/market/tasks/{task_id}/claim", headers=solver_h)

    # Submit
    sub_resp = await client.post(
        f"/v1/market/tasks/{task_id}/submissions",
        json={"summary": "Solution", "skill_recipe": {}, "confidence_score": 0.9},
        headers=solver_h,
    )
    sub_id = sub_resp.json()["submission_id"]

    # Select winner
    await client.post(
        f"/v1/market/tasks/{task_id}/select-winner",
        json={"submission_id": sub_id, "feedback": "Good", "rating": 5},
        headers=poster_h,
    )

    # Audit: check wallets
    poster_wallet = (await client.get("/v1/market/wallet", headers=poster_h)).json()
    solver_wallet = (await client.get("/v1/market/wallet", headers=solver_h)).json()

    # Poster: 100 (mint) - 30 (bounty) = 70
    assert poster_wallet["balance_shl"] == 70.0
    assert poster_wallet["frozen_balance_shl"] == 0.0
    assert poster_wallet["lifetime_spent_shl"] == 30.0

    # Solver: 100 (mint) + 30 (bounty) + 3 (10% bonus) = 133
    # Deposit: -1 lock, +1 refund = net 0
    assert solver_wallet["balance_shl"] == 133.0
    assert solver_wallet["frozen_balance_shl"] == 0.0

    # Audit transactions: total minted should equal total in wallets + burned
    poster_txs = (
        await client.get("/v1/market/wallet/transactions", headers=poster_h)
    ).json()["transactions"]
    solver_txs = (
        await client.get("/v1/market/wallet/transactions", headers=solver_h)
    ).json()["transactions"]

    # Verify mint transactions exist
    all_txs = poster_txs + solver_txs
    mints = [t for t in all_txs if t["tx_type"] == "mint"]
    assert len(mints) == 2  # One per agent

    # Total minted: 200 SHL (100 each)
    # Total bonus minted: 3 SHL
    # Total in wallets: 70 + 133 = 203 SHL
    # Verify conservation: minted = wallets (no burns in this flow)
    total_minted = sum(t["amount_shl"] for t in all_txs if t["tx_type"] in ("mint", "reward"))
    total_wallets = poster_wallet["balance_shl"] + solver_wallet["balance_shl"]
    assert total_minted == total_wallets


async def test_cancel_with_burn_audit(client):
    """Verify burn accounting on task cancellation."""
    # Register new agents
    r1 = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "burn-poster", "display_name": "BurnPoster"},
    )
    poster_key = r1.json()["api_key"]
    poster_h = {"Authorization": f"Bearer {poster_key}"}

    r2 = await client.post(
        "/v1/market/agents/register",
        json={"node_id": "burn-claimer", "display_name": "BurnClaimer"},
    )
    claimer_key = r2.json()["api_key"]
    claimer_h = {"Authorization": f"Bearer {claimer_key}"}

    # Post task (20 SHL) and have it claimed
    task_resp = await client.post(
        "/v1/market/tasks",
        json={"title": "Burn test", "description": "Will cancel", "bounty_shl": 20},
        headers=poster_h,
    )
    task_id = task_resp.json()["task_id"]

    await client.post(f"/v1/market/tasks/{task_id}/claim", headers=claimer_h)

    # Cancel (5% fee = 1 SHL burned)
    await client.delete(f"/v1/market/tasks/{task_id}", headers=poster_h)

    # Poster: 100 - 20 + 19 (refund) = 99
    wallet = (await client.get("/v1/market/wallet", headers=poster_h)).json()
    assert wallet["balance_shl"] == 99.0

    # Verify burn transaction exists
    txs = (await client.get("/v1/market/wallet/transactions", headers=poster_h)).json()["transactions"]
    burns = [t for t in txs if t["tx_type"] == "burn"]
    assert len(burns) == 1
    assert burns[0]["amount_shl"] == 1.0
