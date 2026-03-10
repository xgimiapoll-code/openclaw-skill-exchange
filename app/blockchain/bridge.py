"""Bridge service — deposit/withdraw between on-chain and off-chain."""

import logging
import uuid

import aiosqlite

from app.blockchain.provider import get_web3, get_operator_account, is_blockchain_enabled
from app.blockchain.contracts import get_bridge_contract
from app.models.schemas import shl_to_micro, micro_to_shl
from app.services.wallet_service import get_wallet

logger = logging.getLogger(__name__)


async def verify_deposit(db: aiosqlite.Connection, agent_id: str, tx_hash: str) -> dict:
    """Verify an on-chain deposit transaction and credit the off-chain wallet.

    1. Check tx_hash hasn't been processed before
    2. Verify the transaction on-chain (Deposited event)
    3. Credit the agent's off-chain wallet
    4. Record the bridge request

    Returns bridge request dict.
    """
    if not is_blockchain_enabled():
        raise ValueError("Blockchain not configured")

    # Check duplicate
    cur = await db.execute(
        "SELECT request_id FROM bridge_requests WHERE onchain_tx_hash = ?",
        (tx_hash,),
    )
    if await cur.fetchone():
        raise ValueError("Deposit already processed")

    # Verify on-chain
    w3 = get_web3()
    if not w3:
        raise ValueError("Cannot connect to blockchain")

    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as e:
        raise ValueError(f"Transaction not found: {e}")

    if not receipt or receipt["status"] != 1:
        raise ValueError("Transaction failed or not found")

    # Parse Deposited event
    bridge = get_bridge_contract()
    if not bridge:
        raise ValueError("Bridge contract not configured")

    deposit_events = bridge.events.Deposited().process_receipt(receipt)
    if not deposit_events:
        raise ValueError("No Deposited event found in transaction")

    event = deposit_events[0]
    amount_micro = event["args"]["amount"]
    event_agent_id = event["args"]["agentId"]

    if event_agent_id != agent_id:
        raise ValueError("Deposit agent_id mismatch")

    # Get wallet address from event
    from_address = event["args"]["user"]

    # Credit off-chain wallet
    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")

    await db.execute("SAVEPOINT sp_deposit")
    try:
        await db.execute(
            "UPDATE wallets SET balance = balance + ?, lifetime_earned = lifetime_earned + ? WHERE agent_id = ?",
            (amount_micro, amount_micro, agent_id),
        )

        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, to_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'mint', ?, 'bridge_deposit', 'On-chain deposit')""",
            (tx_id, wallet["wallet_id"], amount_micro, tx_hash),
        )

        # Record bridge request
        request_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO bridge_requests
               (request_id, agent_id, direction, amount, wallet_address, status, onchain_tx_hash, completed_at)
               VALUES (?, ?, 'deposit', ?, ?, 'completed', ?, datetime('now'))""",
            (request_id, agent_id, amount_micro, from_address, tx_hash),
        )

        await db.execute("RELEASE SAVEPOINT sp_deposit")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_deposit")
        raise

    return {
        "request_id": request_id,
        "direction": "deposit",
        "amount_shl": micro_to_shl(amount_micro),
        "wallet_address": from_address,
        "status": "completed",
        "onchain_tx_hash": tx_hash,
    }


async def request_withdraw(
    db: aiosqlite.Connection, agent_id: str, amount_shl: int, wallet_address: str
) -> dict:
    """Request withdrawal from off-chain to on-chain.

    1. Deduct from off-chain wallet
    2. Create pending bridge request
    3. Background task will process the on-chain transfer

    Returns bridge request dict.
    """
    if not is_blockchain_enabled():
        raise ValueError("Blockchain not configured")

    amount_micro = shl_to_micro(amount_shl)

    wallet = await get_wallet(db, agent_id)
    if not wallet:
        raise ValueError("Wallet not found")
    if wallet["balance"] < amount_micro:
        raise ValueError(f"Insufficient balance: have {micro_to_shl(wallet['balance'])} SHL, need {amount_shl} SHL")

    # Validate address format
    w3 = get_web3()
    if not w3:
        raise ValueError("Cannot connect to blockchain")
    if not w3.is_address(wallet_address):
        raise ValueError("Invalid wallet address")
    wallet_address = w3.to_checksum_address(wallet_address)

    await db.execute("SAVEPOINT sp_withdraw")
    try:
        # Deduct from off-chain balance
        await db.execute(
            "UPDATE wallets SET balance = balance - ?, lifetime_spent = lifetime_spent + ? WHERE agent_id = ?",
            (amount_micro, amount_micro, agent_id),
        )

        # Record burn transaction
        tx_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO transactions (tx_id, from_wallet_id, amount, tx_type, reference_id, reference_type, memo)
               VALUES (?, ?, ?, 'burn', ?, 'bridge_withdraw', 'Withdrawal to on-chain')""",
            (tx_id, wallet["wallet_id"], amount_micro, wallet_address),
        )

        # Create pending bridge request
        request_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO bridge_requests
               (request_id, agent_id, direction, amount, wallet_address, status)
               VALUES (?, ?, 'withdraw', ?, ?, 'pending')""",
            (request_id, agent_id, amount_micro, wallet_address),
        )

        await db.execute("RELEASE SAVEPOINT sp_withdraw")
    except Exception:
        await db.execute("ROLLBACK TO SAVEPOINT sp_withdraw")
        raise

    return {
        "request_id": request_id,
        "direction": "withdraw",
        "amount_shl": amount_shl,
        "wallet_address": wallet_address,
        "status": "pending",
    }


async def process_pending_withdrawals(db: aiosqlite.Connection) -> int:
    """Process pending withdrawal requests by sending on-chain transactions.

    Called by background task. Returns number of processed requests.
    """
    if not is_blockchain_enabled():
        return 0

    w3 = get_web3()
    bridge = get_bridge_contract()
    operator = get_operator_account()
    if not w3 or not bridge or not operator:
        return 0

    cur = await db.execute(
        "SELECT * FROM bridge_requests WHERE direction = 'withdraw' AND status = 'pending' ORDER BY created_at ASC LIMIT 10"
    )
    requests = await cur.fetchall()

    processed = 0
    for req in requests:
        req = dict(req)
        try:
            # Mark as processing
            await db.execute(
                "UPDATE bridge_requests SET status = 'processing' WHERE request_id = ?",
                (req["request_id"],),
            )
            await db.commit()

            # Send on-chain transaction
            nonce = w3.eth.get_transaction_count(operator.address)
            tx = bridge.functions.withdraw(
                w3.to_checksum_address(req["wallet_address"]),
                req["amount"],
                req["agent_id"],
            ).build_transaction({
                "from": operator.address,
                "nonce": nonce,
                "gas": 100000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
                "chainId": w3.eth.chain_id,
            })

            signed = operator.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            if receipt["status"] == 1:
                await db.execute(
                    """UPDATE bridge_requests SET status = 'completed',
                       onchain_tx_hash = ?, completed_at = datetime('now')
                       WHERE request_id = ?""",
                    (tx_hash.hex(), req["request_id"]),
                )
                processed += 1
            else:
                await db.execute(
                    """UPDATE bridge_requests SET status = 'failed',
                       error_message = 'Transaction reverted'
                       WHERE request_id = ?""",
                    (req["request_id"],),
                )

        except Exception as e:
            logger.error("Failed to process withdrawal %s: %s", req["request_id"], e)
            await db.execute(
                """UPDATE bridge_requests SET status = 'failed',
                   error_message = ? WHERE request_id = ?""",
                (str(e)[:500], req["request_id"]),
            )

        await db.commit()

    return processed


async def get_bridge_requests(
    db: aiosqlite.Connection, agent_id: str,
    direction: str | None = None, status: str | None = None,
    page: int = 1, page_size: int = 20,
) -> tuple[list[dict], int]:
    """Get bridge requests for an agent."""
    conditions = ["agent_id = ?"]
    params: list = [agent_id]

    if direction:
        conditions.append("direction = ?")
        params.append(direction)
    if status:
        conditions.append("status = ?")
        params.append(status)

    where = " WHERE " + " AND ".join(conditions)

    cur = await db.execute(f"SELECT COUNT(*) as cnt FROM bridge_requests{where}", params)
    total = (await cur.fetchone())["cnt"]

    offset = (page - 1) * page_size
    cur = await db.execute(
        f"SELECT * FROM bridge_requests{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    rows = await cur.fetchall()

    return [
        {
            "request_id": r["request_id"],
            "direction": r["direction"],
            "amount_shl": micro_to_shl(r["amount"]),
            "wallet_address": r["wallet_address"],
            "status": r["status"],
            "onchain_tx_hash": r.get("onchain_tx_hash"),
            "error_message": r.get("error_message"),
            "created_at": r.get("created_at", ""),
            "completed_at": r.get("completed_at"),
        }
        for r in rows
    ], total
