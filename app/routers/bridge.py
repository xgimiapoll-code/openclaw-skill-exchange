"""Bridge endpoints — deposit/withdraw between on-chain and off-chain."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.deps import get_current_agent
from app.blockchain.provider import get_chain_status, is_blockchain_enabled
from app.blockchain.bridge import (
    get_bridge_requests,
    request_withdraw,
    verify_deposit,
)
from app.blockchain.settlement import (
    create_settlement_batch,
    get_settlement_batches,
    verify_transaction_in_batch,
)
from app.db import get_db

router = APIRouter(prefix="/bridge", tags=["bridge"])


class DepositRequest(BaseModel):
    tx_hash: str = Field(..., min_length=1, description="On-chain transaction hash of the deposit")


class WithdrawRequest(BaseModel):
    amount_shl: int = Field(..., gt=0, description="Amount in SHL to withdraw")
    wallet_address: str = Field(..., min_length=1, description="On-chain wallet address to receive tokens")


@router.get("/status")
async def bridge_status():
    """Get blockchain bridge status and configuration."""
    return get_chain_status()


@router.post("/deposit")
async def deposit(
    body: DepositRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Verify an on-chain deposit and credit off-chain wallet.

    After sending SHL tokens to the bridge contract, call this endpoint
    with the transaction hash to credit your off-chain balance.
    """
    if not is_blockchain_enabled():
        raise HTTPException(status_code=503, detail="Blockchain bridge not configured")

    try:
        result = await verify_deposit(db, agent["agent_id"], body.tx_hash)
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.post("/withdraw")
async def withdraw(
    body: WithdrawRequest,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Request withdrawal from off-chain to on-chain wallet.

    Deducts from your off-chain balance immediately.
    The on-chain transfer is processed asynchronously (usually within minutes).
    """
    if not is_blockchain_enabled():
        raise HTTPException(status_code=503, detail="Blockchain bridge not configured")

    try:
        result = await request_withdraw(
            db, agent["agent_id"], body.amount_shl, body.wallet_address
        )
        await db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


@router.get("/requests")
async def list_bridge_requests(
    direction: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """List your deposit/withdraw history."""
    requests, total = await get_bridge_requests(
        db, agent["agent_id"], direction, status, page, page_size
    )
    return {"requests": requests, "total": total, "page": page, "page_size": page_size}


# ── Settlement endpoints ──


@router.get("/settlement/batches")
async def list_settlement_batches(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: aiosqlite.Connection = Depends(get_db),
):
    """List settlement batches (public — anyone can verify)."""
    batches, total = await get_settlement_batches(db, page, page_size)
    return {"batches": batches, "total": total, "page": page, "page_size": page_size}


@router.get("/settlement/verify/{tx_id}")
async def verify_transaction(
    tx_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    """Verify a transaction is included in a settlement batch with merkle proof."""
    try:
        result = await verify_transaction_in_batch(db, tx_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return result


@router.post("/settlement/create")
async def trigger_settlement(
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Manually trigger creation of a settlement batch (admin/debug)."""
    batch = await create_settlement_batch(db, min_batch_size=1)
    if not batch:
        return {"message": "No unsettled transactions"}
    return batch
