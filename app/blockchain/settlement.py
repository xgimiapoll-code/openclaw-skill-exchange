"""Settlement service — batch off-chain transactions and record merkle roots on-chain."""

import hashlib
import logging
import uuid

import aiosqlite

from app.blockchain.provider import get_web3, get_operator_account, is_blockchain_enabled
from app.blockchain.contracts import get_bridge_contract

logger = logging.getLogger(__name__)


def _hash_leaf(tx_id: str, amount: int, tx_type: str, created_at: str) -> bytes:
    """Hash a single transaction into a merkle leaf."""
    data = f"{tx_id}:{amount}:{tx_type}:{created_at}"
    return hashlib.sha256(data.encode()).digest()


def _hash_pair(left: bytes, right: bytes) -> bytes:
    """Hash two merkle nodes together."""
    if left > right:
        left, right = right, left
    return hashlib.sha256(left + right).digest()


def compute_merkle_root(leaves: list[bytes]) -> bytes:
    """Compute merkle root from leaf hashes."""
    if not leaves:
        return b"\x00" * 32

    # Pad to power of 2
    while len(leaves) & (len(leaves) - 1):
        leaves.append(leaves[-1])

    layer = leaves
    while len(layer) > 1:
        next_layer = []
        for i in range(0, len(layer), 2):
            next_layer.append(_hash_pair(layer[i], layer[i + 1]))
        layer = next_layer

    return layer[0]


def compute_merkle_proof(leaves: list[bytes], index: int) -> list[bytes]:
    """Compute merkle proof for a leaf at the given index."""
    if not leaves or index >= len(leaves):
        return []

    # Pad to power of 2
    while len(leaves) & (len(leaves) - 1):
        leaves.append(leaves[-1])

    proof = []
    layer = leaves
    idx = index

    while len(layer) > 1:
        sibling_idx = idx ^ 1  # XOR to find sibling
        if sibling_idx < len(layer):
            proof.append(layer[sibling_idx])

        next_layer = []
        for i in range(0, len(layer), 2):
            next_layer.append(_hash_pair(layer[i], layer[i + 1]))
        layer = next_layer
        idx //= 2

    return proof


def verify_merkle_proof(leaf: bytes, proof: list[bytes], root: bytes) -> bool:
    """Verify a merkle proof."""
    current = leaf
    for sibling in proof:
        current = _hash_pair(current, sibling)
    return current == root


async def create_settlement_batch(db: aiosqlite.Connection, min_batch_size: int = 1) -> dict | None:
    """Create a settlement batch from unsettled transactions.

    Returns batch dict or None if not enough transactions.
    """
    # Find unsettled transactions
    cur = await db.execute(
        """SELECT tx_id, amount, tx_type, created_at FROM transactions
           WHERE settlement_batch_id IS NULL
           ORDER BY created_at ASC"""
    )
    txs = await cur.fetchall()

    if len(txs) < min_batch_size:
        return None

    # Compute merkle root
    leaves = [_hash_leaf(t["tx_id"], t["amount"], t["tx_type"], t["created_at"]) for t in txs]
    root = compute_merkle_root(leaves)

    batch_id = str(uuid.uuid4())
    merkle_root_hex = root.hex()

    first_tx = txs[0]["tx_id"]
    last_tx = txs[-1]["tx_id"]

    await db.execute(
        """INSERT INTO settlement_batches
           (batch_id, merkle_root, tx_count, start_tx_id, end_tx_id, status)
           VALUES (?, ?, ?, ?, ?, 'pending')""",
        (batch_id, merkle_root_hex, len(txs), first_tx, last_tx),
    )

    # Mark transactions as settled
    tx_ids = [t["tx_id"] for t in txs]
    placeholders = ",".join(["?"] * len(tx_ids))
    await db.execute(
        f"UPDATE transactions SET settlement_batch_id = ? WHERE tx_id IN ({placeholders})",
        [batch_id] + tx_ids,
    )

    await db.commit()

    return {
        "batch_id": batch_id,
        "merkle_root": merkle_root_hex,
        "tx_count": len(txs),
        "status": "pending",
    }


async def submit_batch_onchain(db: aiosqlite.Connection, batch_id: str) -> dict:
    """Submit a settlement batch merkle root to the on-chain contract.

    Returns updated batch dict.
    """
    if not is_blockchain_enabled():
        raise ValueError("Blockchain not configured")

    cur = await db.execute(
        "SELECT * FROM settlement_batches WHERE batch_id = ?", (batch_id,)
    )
    batch = await cur.fetchone()
    if not batch:
        raise ValueError("Batch not found")
    batch = dict(batch)

    if batch["status"] != "pending":
        raise ValueError(f"Batch status is {batch['status']}, expected pending")

    w3 = get_web3()
    bridge = get_bridge_contract()
    operator = get_operator_account()
    if not w3 or not bridge or not operator:
        raise ValueError("Blockchain connection failed")

    # Convert hex string to bytes32
    merkle_root_bytes = bytes.fromhex(batch["merkle_root"])

    try:
        nonce = w3.eth.get_transaction_count(operator.address)
        tx = bridge.functions.settleBatch(
            merkle_root_bytes,
            batch["tx_count"],
        ).build_transaction({
            "from": operator.address,
            "nonce": nonce,
            "gas": 80000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(0.001, "gwei"),
            "chainId": w3.eth.chain_id,
        })

        signed = operator.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt["status"] == 1:
            await db.execute(
                """UPDATE settlement_batches SET status = 'confirmed',
                   onchain_tx_hash = ?, confirmed_at = datetime('now')
                   WHERE batch_id = ?""",
                (tx_hash.hex(), batch_id),
            )
            await db.commit()
            return {
                "batch_id": batch_id,
                "status": "confirmed",
                "onchain_tx_hash": tx_hash.hex(),
            }
        else:
            await db.execute(
                "UPDATE settlement_batches SET status = 'failed' WHERE batch_id = ?",
                (batch_id,),
            )
            await db.commit()
            raise ValueError("Settlement transaction reverted")

    except ValueError:
        raise
    except Exception as e:
        await db.execute(
            "UPDATE settlement_batches SET status = 'failed' WHERE batch_id = ?",
            (batch_id,),
        )
        await db.commit()
        raise ValueError(f"Settlement failed: {e}")


async def get_settlement_batches(
    db: aiosqlite.Connection, page: int = 1, page_size: int = 20
) -> tuple[list[dict], int]:
    """Get settlement batch history."""
    cur = await db.execute("SELECT COUNT(*) as cnt FROM settlement_batches")
    total = (await cur.fetchone())["cnt"]

    offset = (page - 1) * page_size
    cur = await db.execute(
        "SELECT * FROM settlement_batches ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    rows = await cur.fetchall()

    results = []
    for r in rows:
        r = dict(r)
        results.append({
            "batch_id": r["batch_id"],
            "merkle_root": r["merkle_root"],
            "tx_count": r["tx_count"],
            "onchain_tx_hash": r.get("onchain_tx_hash"),
            "status": r["status"],
            "created_at": r.get("created_at", ""),
            "confirmed_at": r.get("confirmed_at"),
        })
    return results, total


async def verify_transaction_in_batch(
    db: aiosqlite.Connection, tx_id: str
) -> dict:
    """Verify a transaction is included in a settlement batch and return proof."""
    cur = await db.execute(
        "SELECT * FROM transactions WHERE tx_id = ?", (tx_id,)
    )
    tx = await cur.fetchone()
    if not tx:
        raise ValueError("Transaction not found")
    tx = dict(tx)

    if not tx.get("settlement_batch_id"):
        return {"verified": False, "reason": "Transaction not yet settled"}

    batch_id = tx["settlement_batch_id"]

    # Get all transactions in this batch
    cur = await db.execute(
        """SELECT tx_id, amount, tx_type, created_at FROM transactions
           WHERE settlement_batch_id = ?
           ORDER BY created_at ASC""",
        (batch_id,),
    )
    batch_txs = await cur.fetchall()

    leaves = [_hash_leaf(t["tx_id"], t["amount"], t["tx_type"], t["created_at"]) for t in batch_txs]
    target_leaf = _hash_leaf(tx["tx_id"], tx["amount"], tx["tx_type"], tx["created_at"])

    # Find index
    try:
        index = next(i for i, l in enumerate(leaves) if l == target_leaf)
    except StopIteration:
        return {"verified": False, "reason": "Transaction not found in batch leaves"}

    root = compute_merkle_root(list(leaves))
    proof = compute_merkle_proof(list(leaves), index)

    # Verify
    verified = verify_merkle_proof(target_leaf, proof, root)

    # Get batch info
    cur = await db.execute(
        "SELECT merkle_root, onchain_tx_hash, status FROM settlement_batches WHERE batch_id = ?",
        (batch_id,),
    )
    batch = dict(await cur.fetchone())

    return {
        "verified": verified,
        "tx_id": tx_id,
        "batch_id": batch_id,
        "merkle_root": batch["merkle_root"],
        "onchain_tx_hash": batch.get("onchain_tx_hash"),
        "batch_status": batch["status"],
        "proof": [p.hex() for p in proof],
        "leaf": target_leaf.hex(),
    }
