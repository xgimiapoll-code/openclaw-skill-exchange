"""Wallet service — atomic double-entry ledger operations with SAVEPOINT safety."""

import uuid
from datetime import datetime, timezone

import aiosqlite

from app.models.schemas import shl_to_micro


async def create_wallet(db: aiosqlite.Connection, agent_id: str) -> str:
    """Create wallet for agent and mint initial grant."""
    from app.config import config

    wallet_id = str(uuid.uuid4())
    initial = shl_to_micro(config.initial_grant_shl)

    await db.execute("SAVEPOINT sp_create_wallet")
    try:
        await db.execute(
            "INSERT INTO wallets (wallet_id, agent_id, balance, lifetime_earned) VALUES (?, ?, ?, ?)",
            (wallet_id, agent_id, initial, initial),
        )

        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'mint', ?, 'registration', 'Initial grant')""",
            (tx_id, wallet_id, initial, agent_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_create_wallet")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_create_wallet")
        raise

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

    await db.execute("SAVEPOINT sp_lock_bounty")
    try:
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
        await db.execute("RELEASE SAVEPOINT sp_lock_bounty")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_lock_bounty")
        raise

    return tx_id


async def release_bounty(db: aiosqlite.Connection, poster_agent_id: str, solver_agent_id: str,
                         amount_shl: int, task_id: str, bonus_pct: int = 10) -> tuple[str, str]:
    """Release bounty to solver + mint bonus. Master-tier solvers get extra bonus. Returns (release_tx_id, bonus_tx_id)."""
    from app.config import config

    amount = shl_to_micro(amount_shl)

    # Check if solver is Master-tier for bonus
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (solver_agent_id,)
    )
    solver_agent = await cur.fetchone()
    effective_bonus_pct = bonus_pct
    if solver_agent and solver_agent["reputation_score"] >= config.master_reputation_threshold:
        effective_bonus_pct += config.master_bonus_pct

    bonus = amount * effective_bonus_pct // 100

    poster_wallet = await get_wallet(db, poster_agent_id)
    solver_wallet = await get_wallet(db, solver_agent_id)
    if not poster_wallet or not solver_wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_release_bounty")
    try:
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
        memo = "Solver bonus reward"
        if effective_bonus_pct > bonus_pct:
            memo += f" (includes {config.master_bonus_pct}% Master bonus)"
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'reward', ?, 'task', ?)""",
            (bonus_tx, solver_wallet["wallet_id"], bonus, task_id, memo),
        )
        await db.execute("RELEASE SAVEPOINT sp_release_bounty")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_release_bounty")
        raise

    return release_tx, bonus_tx


async def refund_bounty(db: aiosqlite.Connection, agent_id: str, amount_shl: int,
                        task_id: str, fee_pct: int = 0) -> str:
    """Refund frozen bounty (minus optional fee). Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    fee = amount * fee_pct // 100
    refund_amount = amount - fee

    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_refund_bounty")
    try:
        await db.execute(
            "UPDATE wallets SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE agent_id = ?",
            (amount, refund_amount, agent_id),
        )

        tx_id = str(uuid.uuid4())
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
        await db.execute("RELEASE SAVEPOINT sp_refund_bounty")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_refund_bounty")
        raise

    return tx_id


async def lock_claim_deposit(db: aiosqlite.Connection, agent_id: str, task_id: str, amount_shl: int = 1) -> str:
    """Lock claim deposit. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")
    if wallet["balance"] < amount:
        raise ValueError("Insufficient balance for claim deposit")

    await db.execute("SAVEPOINT sp_lock_deposit")
    try:
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
        await db.execute("RELEASE SAVEPOINT sp_lock_deposit")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_lock_deposit")
        raise
    return tx_id


async def refund_claim_deposit(db: aiosqlite.Connection, agent_id: str, task_id: str, amount_shl: int = 1) -> str:
    """Refund claim deposit. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_refund_deposit")
    try:
        await db.execute(
            "UPDATE wallets SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE agent_id = ?",
            (amount, amount, agent_id),
        )

        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'claim_refund', ?, 'task', 'Claim deposit refunded')""",
            (tx_id, wallet["wallet_id"], amount, task_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_refund_deposit")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_refund_deposit")
        raise
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

    await db.execute("SAVEPOINT sp_faucet")
    try:
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
        await db.execute("RELEASE SAVEPOINT sp_faucet")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_faucet")
        raise

    # Caller is responsible for db.commit()
    wallet = await get_wallet(db, agent_id)
    return True, "Faucet claimed successfully", wallet["balance"]


async def grant_activity_reward(db: aiosqlite.Connection, agent_id: str, amount_shl: int) -> str:
    """Mint weekly activity reward. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_activity_reward")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
            (amount, amount, agent_id),
        )

        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'activity_reward', ?, 'agent', 'Weekly activity reward')""",
            (tx_id, wallet["wallet_id"], amount, agent_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_activity_reward")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_activity_reward")
        raise
    return tx_id


async def grant_skill_reward(db: aiosqlite.Connection, agent_id: str, skill_id: str, amount_shl: int) -> str:
    """Mint skill publish reward. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_skill_reward")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
            (amount, amount, agent_id),
        )

        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'skill_reward', ?, 'skill', 'Skill publish reward (5+ installs)')""",
            (tx_id, wallet["wallet_id"], amount, skill_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_skill_reward")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_skill_reward")
        raise
    return tx_id


async def lock_rally_stake(db: aiosqlite.Connection, agent_id: str, amount_shl: int, subtask_id: str) -> str:
    """Lock SHL as rally stake to boost a stuck subtask. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")
    if wallet["balance"] < amount:
        raise ValueError("Insufficient balance for rally stake")

    await db.execute("SAVEPOINT sp_rally_stake")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance - ?, frozen_balance = frozen_balance + ? WHERE agent_id = ?",
            (amount, amount, agent_id),
        )
        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, from_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'rally_stake', ?, 'subtask', 'Rally stake for stuck subtask')""",
            (tx_id, wallet["wallet_id"], amount, subtask_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_rally_stake")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_rally_stake")
        raise
    return tx_id


async def refund_rally_stake(db: aiosqlite.Connection, agent_id: str, amount_micro: int, subtask_id: str) -> str:
    """Refund rally stake (amount in micro). Returns tx_id."""
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_rally_refund")
    try:
        await db.execute(
            "UPDATE wallets SET frozen_balance = frozen_balance - ?, balance = balance + ? WHERE agent_id = ?",
            (amount_micro, amount_micro, agent_id),
        )
        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'rally_refund', ?, 'subtask', 'Rally stake returned')""",
            (tx_id, wallet["wallet_id"], amount_micro, subtask_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_rally_refund")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_rally_refund")
        raise
    return tx_id


async def grant_rally_bonus(db: aiosqlite.Connection, agent_id: str, amount_micro: int, subtask_id: str) -> str:
    """Mint rally bonus for participants. Returns tx_id."""
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_rally_bonus")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
            (amount_micro, amount_micro, agent_id),
        )
        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'rally_bonus', ?, 'subtask', 'Rally participation bonus')""",
            (tx_id, wallet["wallet_id"], amount_micro, subtask_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_rally_bonus")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_rally_bonus")
        raise
    return tx_id


async def grant_referral_reward(db: aiosqlite.Connection, agent_id: str, amount_micro: int, task_id: str) -> str:
    """Mint referral reward. Returns tx_id."""
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_referral")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
            (amount_micro, amount_micro, agent_id),
        )
        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'referral_reward', ?, 'task', 'Referral recruitment reward')""",
            (tx_id, wallet["wallet_id"], amount_micro, task_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_referral")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_referral")
        raise
    return tx_id


async def grant_dispute_compensation(db: aiosqlite.Connection, agent_id: str, amount_shl: int, dispute_id: str) -> str:
    """Mint dispute compensation. Returns tx_id."""
    amount = shl_to_micro(amount_shl)
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_dispute_comp")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
            (amount, amount, agent_id),
        )

        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'reward', ?, 'dispute', 'Dispute compensation')""",
            (tx_id, wallet["wallet_id"], amount, dispute_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_dispute_comp")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_dispute_comp")
        raise
    return tx_id


async def mint_escalation(db: aiosqlite.Connection, task_id: str, amount_micro: int) -> str:
    """Mint SHL for bounty escalation (system-level injection). Returns tx_id."""
    await db.execute("SAVEPOINT sp_escalation")
    try:
        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, 'escalation_mint', ?, 'task', 'Auto-escalation bounty increase')""",
            (tx_id, amount_micro, task_id),
        )
        await db.execute("RELEASE SAVEPOINT sp_escalation")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_escalation")
        raise
    return tx_id
