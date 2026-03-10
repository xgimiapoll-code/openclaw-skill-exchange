"""Smart contract ABIs and interface helpers."""

from app.blockchain.provider import get_web3, is_blockchain_enabled

# Minimal ABIs — only the functions we call from Python
# Full ABIs would come from Hardhat/Foundry compilation

TOKEN_ABI = [
    {
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "mint",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

BRIDGE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "agentId", "type": "string"},
        ],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "merkleRoot", "type": "bytes32"},
            {"name": "txCount", "type": "uint256"},
        ],
        "name": "settleBatch",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "bridgeBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "latestBatchId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "batchId", "type": "uint256"}],
        "name": "getBatch",
        "outputs": [
            {"name": "merkleRoot", "type": "bytes32"},
            {"name": "txCount", "type": "uint256"},
            {"name": "timestamp", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "user", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "agentId", "type": "string"},
            {"indexed": False, "name": "timestamp", "type": "uint256"},
        ],
        "name": "Deposited",
        "type": "event",
    },
]


def get_token_contract():
    """Get SHLToken contract instance."""
    w3 = get_web3()
    if not w3:
        return None
    from app.config import config
    if not config.token_contract_address:
        return None
    return w3.eth.contract(
        address=w3.to_checksum_address(config.token_contract_address),
        abi=TOKEN_ABI,
    )


def get_bridge_contract():
    """Get SHLBridge contract instance."""
    w3 = get_web3()
    if not w3:
        return None
    from app.config import config
    if not config.bridge_contract_address:
        return None
    return w3.eth.contract(
        address=w3.to_checksum_address(config.bridge_contract_address),
        abi=BRIDGE_ABI,
    )
