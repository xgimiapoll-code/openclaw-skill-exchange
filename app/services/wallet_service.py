"""Wallet service — atomic double-entry ledger operations."""

import uuid
from datetime import datetime, timezone

import aiosqlite

from app.models.schemas import shl_to_micro


async def create_wallet(db: aiosqlite.Connection, agent_id: str) -> str:
    """Create wallet for agent and mint initial grant."""
    from app.config import config

    wallet_id = str(uuid.uuid4())
    initial = shl_to_micro(config.initial_grant_shl)

    await db.execute(
        "INSERT INTO wallets (wallet_id, agent_id, balance, lifetime_earned) VALUES (?, ?, ?, ?)",
        (wallet_id, agent_id, initial, initial),
    )

    # Record mint transaction
    tx_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, 'mint', ?, 'registration', 'Initial grant')""",
        (tx_id, wallet_id, initial, agent_id),
    )

    return wallet_id


async def get_wallet(db: aiosqlite.Connection, agent_id: str) -> dict | None:
    """Get wallet for agent."""
    cur = await db.execute("SELECT * FROM wallets WHERE agent_id = ?", (agent_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def lock_bounty(db: aiosqlite.Connection, agent_id: str, amount_shl: int, task_id: str) -> str:
    """Lock SHL for a bounty. Returns tx_id. Raises ValueError if insufficient balance."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")
    if wallet["balance"] < amount:
        raise ValueError(f"Insufficient balance: have {wallet['balance']}, need {amount}")

    await db.execute(
        "UPDATE wallets SET balance = balance - ?, frozen_balance = frozen_balance + ? WHERE agent_id = ?",
        (amount, amount, agent_id),
    )

    tx_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, from_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, 'bounty_lock', ?, 'task', 'Bounty locked for task')""",
        (tx_id, wallet["wallet_id"], amount, task_id),
    )

    return tx_id


async def release_bounty(db: aiosqlite.Connection, poster_agent_id: str, solver_agent_id: str,
                         amount_shl: int, task_id: str, bonus_pct: int = 10) -> tuple[str, str]:
    """Release bounty to solver + mint bonus. Returns (release_tx_id, bonus_tx_id)."""
    amount = shl_to_micro(amount_shl)
    bonus = amount * bonus_pct // 100

    poster_wallet = await get_wallet(db, poster_agent_id)
    solver_wallet = await get_wallet(db, solver_agent_id)
    if not poster_wallet or not solver_wallet:
        raise ValueError("Wallet not found")

    # Unfreeze from poster
    await db.execute(
        "UPDATE wallets SET frozen_balance = frozen_balance - ? WHERE agent_id = ?",
        (amount, poster_agent_id),
    )
    # Update poster lifetime_spent
    await db.execute(
        "UPDATE wallets SET lifetime_spent = lifetime_spent + ? WHERE agent_id = ?",
        (amount, poster_agent_id),
    )

    # Credit solver: bounty + bonus
    total_credit = amount + bonus
    await db.execute(
        "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
        (total_credit, total_credit, solver_agent_id),
    )

    # Transaction: bounty release
    release_tx = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, from_wallet_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, ?, 'bounty_release', ?, 'task', 'Bounty released to solver')""",
        (release_tx, poster_wallet["wallet_id"], solver_wallet["wallet_id"], amount, task_id),
    )

    # Transaction: bonus mint
    bonus_tx = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, 'reward', ?, 'task', 'Solver bonus reward')""",
        (bonus_tx, solver_wallet["wallet_id"], bonus, task_id),
    )

    return release_tx, bonus_tx


async def refund_bounty(db: aiosqlite.Connection, agent_id: str, amount_shl: int,
                        task_id: str, fee_pct: int = 0) -> str:
    """Refund frozen bounty (minus optional fee). Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    fee = amount * fee_pct // 100
    refund_amount = amount - fee

    await db.execute(
        "UPDATE wallets SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE agent_id = ?",
        (amount, refund_amount, agent_id),
    )

    tx_id = str(uuid.uuid4())
    wallet = await get_wallet(db, agent_id)
    await db.execute(
        """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, 'bounty_refund', ?, 'task', ?)""",
        (tx_id, wallet["wallet_id"], refund_amount, task_id,
         f"Bounty refund (fee: {fee_pct}%)"),
    )

    if fee > 0:
        burn_tx = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, from_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'burn', ?, 'task', 'Cancellation fee burned')""",
            (burn_tx, wallet["wallet_id"], fee, task_id),
        )

    return tx_id


async def lock_claim_deposit(db: aiosqlite.Connection, agent_id: str, task_id: str, amount_shl: int = 1) -> str:
    """Lock claim deposit. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")
    if wallet["balance"] < amount:
        raise ValueError("Insufficient balance for claim deposit")

    await db.execute(
        "UPDATE wallets SET balance = balance - ?, frozen_balance = frozen_balance + ? WHERE agent_id = ?",
        (amount, amount, agent_id),
    )

    tx_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, from_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, 'claim_deposit', ?, 'task', 'Claim deposit locked')""",
        (tx_id, wallet["wallet_id"], amount, task_id),
    )
    return tx_id


async def refund_claim_deposit(db: aiosqlite.Connection, agent_id: str, task_id: str, amount_shl: int = 1) -> str:
    """Refund claim deposit. Returns tx_id."""
    amount = shl_to_micro(amount_shl)

    await db.execute(
        "UPDATE wallets SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE agent_id = ?",
        (amount, amount, agent_id),
    )

    wallet = await get_wallet(db, agent_id)
    tx_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
           VALUES (?, ?, ?, 'claim_refund', ?, 'task', 'Claim deposit refunded')""",
        (tx_id, wallet["wallet_id"], amount, task_id),
    )
    return tx_id


async def claim_faucet(db: aiosqlite.Connection, agent_id: str, amount_shl: int = 10) -> tuple[bool, str, int]:
    """Claim daily faucet. Returns (success, message, new_balance_micro)."""
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        return False, "Wallet not found", 0

    now = datetime.now(timezone.utc)
    last_claim = wallet.get("last_faucet_claim")
    if last_claim:
        last_dt = datetime.fromisoformat(last_claim.replace("Z", "+00:00")) if "T" in last_claim else datetime.fromisoformat(last_claim + "+00:00")
        if (now - last_dt).total_seconds() < 86400:
            return False, "Faucet already claimed today", wallet["balance"]

    amount = shl_to_micro(amount_shl)
    await db.execute(
        "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ?, last_faucet_claim = ? WHERE agent_id = ?",
        (amount, amount, now.isoformat(), agent_id),
    )

    tx_id = str(uuid.uuid4())
    await db.execute(
        """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, memo)
           VALUES (?, ?, ?, 'faucet', 'Daily faucet claim')""",
        (tx_id, wallet["wallet_id"], amount),
    )

    await db.commit()
    wallet = await get_wallet(db, agent_id)
    return True, "Faucet claimed successfully", wallet["balance"]
