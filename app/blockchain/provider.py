"""Web3 provider — connection management and blockchain status."""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_web3_available = False
try:
    from web3 import Web3
    from eth_account import Account
    _web3_available = True
except ImportError:
    Web3 = None
    Account = None


def is_web3_installed() -> bool:
    return _web3_available


def is_blockchain_enabled() -> bool:
    """Check if blockchain is configured and available."""
    if not _web3_available:
        return False
    from app.config import config
    return bool(config.chain_rpc_url and config.bridge_operator_key)


def get_web3():
    """Get Web3 instance. Returns None if not configured."""
    if not is_blockchain_enabled():
        return None
    from app.config import config
    w3 = Web3(Web3.HTTPProvider(config.chain_rpc_url))
    if not w3.is_connected():
        logger.warning("Web3 not connected to %s", config.chain_rpc_url)
        return None
    return w3


def get_operator_account():
    """Get the bridge operator account for signing transactions."""
    if not is_blockchain_enabled():
        return None
    from app.config import config
    return Account.from_key(config.bridge_operator_key)


def get_chain_status() -> dict:
    """Get current blockchain connection status."""
    if not _web3_available:
        return {"enabled": False, "reason": "web3 package not installed"}
    if not is_blockchain_enabled():
        return {"enabled": False, "reason": "blockchain not configured"}

    w3 = get_web3()
    if not w3:
        return {"enabled": False, "reason": "cannot connect to RPC"}

    from app.config import config
    return {
        "enabled": True,
        "chain_id": config.chain_id,
        "rpc_url": config.chain_rpc_url,
        "block_number": w3.eth.block_number,
        "operator_address": get_operator_account().address if get_operator_account() else None,
    }
