"""Wallet and transaction endpoints."""

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException

from app.auth.deps import get_current_agent
from app.config import config
from app.db import get_db
from app.models.schemas import (
    FaucetOut,
    TransactionOut,
    WalletOut,
    micro_to_shl,
)
from app.services.wallet_service import claim_faucet, get_wallet

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("", response_model=WalletOut)
async def get_my_wallet(
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get current agent's wallet balance."""
    wallet = await get_wallet(db, agent["agent_id"])
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return WalletOut.from_row(wallet)


@router.get("/transactions")
async def get_transactions(
    page: int = 1,
    page_size: int = 50,
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Get transaction history for current agent's wallet."""
    wallet = await get_wallet(db, agent["agent_id"])
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    wid = wallet["wallet_id"]

    # Total count for pagination
    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM transactions WHERE from_wallet_id = ? OR to_wallet_id = ?",
        (wid, wid),
    )
    total = (await cur.fetchone())["cnt"]

    offset = (page - 1) * page_size
    cur = await db.execute(
        """SELECT * FROM transactions
           WHERE from_wallet_id = ? OR to_wallet_id = ?
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        (wid, wid, page_size, offset),
    )
    rows = await cur.fetchall()
    return {
        "transactions": [TransactionOut.from_row(dict(r)) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/claim-faucet", response_model=FaucetOut)
async def do_claim_faucet(
    agent: dict = Depends(get_current_agent),
    db: aiosqlite.Connection = Depends(get_db),
):
    """Claim daily faucet (10 SHL)."""
    success, message, new_balance = await claim_faucet(
        db, agent["agent_id"], config.daily_faucet_shl
    )
    if success:
        await db.commit()
    return FaucetOut(
        success=success,
        amount_shl=config.daily_faucet_shl if success else 0,
        new_balance_shl=micro_to_shl(new_balance),
        message=message,
    )
