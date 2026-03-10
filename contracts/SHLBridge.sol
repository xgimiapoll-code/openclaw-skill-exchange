// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/**
 * @title SHLBridge — Off-chain ↔ On-chain Bridge (贝壳桥)
 * @notice Handles deposits (on-chain → off-chain) and withdrawals (off-chain → on-chain).
 *         Also records settlement batch merkle roots for off-chain transaction proofs.
 *
 *         Deposit flow:
 *           1. User approves SHL tokens to this contract
 *           2. User calls deposit(amount, agentId)
 *           3. Tokens held in contract, event emitted
 *           4. Backend detects event, credits off-chain wallet
 *
 *         Withdraw flow:
 *           1. User requests withdrawal via off-chain API
 *           2. Backend deducts off-chain balance
 *           3. Operator calls withdraw(to, amount, agentId)
 *           4. Tokens released to user's wallet
 */
contract SHLBridge is Ownable {
    IERC20 public immutable shlToken;

    uint256 public totalDeposited;
    uint256 public totalWithdrawn;
    uint256 public latestBatchId;

    struct SettlementBatch {
        bytes32 merkleRoot;
        uint256 txCount;
        uint256 timestamp;
    }

    mapping(uint256 => SettlementBatch) public batches;

    event Deposited(
        address indexed user,
        uint256 amount,
        string agentId,
        uint256 timestamp
    );

    event Withdrawn(
        address indexed user,
        uint256 amount,
        string agentId,
        uint256 timestamp
    );

    event BatchSettled(
        uint256 indexed batchId,
        bytes32 merkleRoot,
        uint256 txCount,
        uint256 timestamp
    );

    constructor(address _shlToken) Ownable(msg.sender) {
        shlToken = IERC20(_shlToken);
    }

    /// @notice Deposit SHL tokens from on-chain to off-chain wallet.
    ///         User must approve this contract first.
    /// @param amount Amount in micro-SHL (6 decimals)
    /// @param agentId The off-chain agent ID to credit
    function deposit(uint256 amount, string calldata agentId) external {
        require(amount > 0, "Amount must be > 0");
        require(bytes(agentId).length > 0, "Agent ID required");

        shlToken.transferFrom(msg.sender, address(this), amount);
        totalDeposited += amount;

        emit Deposited(msg.sender, amount, agentId, block.timestamp);
    }

    /// @notice Withdraw SHL tokens from off-chain to on-chain wallet.
    ///         Only bridge operator can call (after verifying off-chain deduction).
    /// @param to Recipient wallet address
    /// @param amount Amount in micro-SHL
    /// @param agentId The off-chain agent ID that requested withdrawal
    function withdraw(
        address to,
        uint256 amount,
        string calldata agentId
    ) external onlyOwner {
        require(amount > 0, "Amount must be > 0");
        require(to != address(0), "Invalid address");

        shlToken.transfer(to, amount);
        totalWithdrawn += amount;

        emit Withdrawn(to, amount, agentId, block.timestamp);
    }

    /// @notice Record a settlement batch merkle root on-chain.
    ///         This proves the state of off-chain transactions at a point in time.
    /// @param merkleRoot Root hash of the transaction batch merkle tree
    /// @param txCount Number of transactions in the batch
    function settleBatch(
        bytes32 merkleRoot,
        uint256 txCount
    ) external onlyOwner {
        latestBatchId++;
        batches[latestBatchId] = SettlementBatch({
            merkleRoot: merkleRoot,
            txCount: txCount,
            timestamp: block.timestamp
        });

        emit BatchSettled(latestBatchId, merkleRoot, txCount, block.timestamp);
    }

    /// @notice Verify a settlement batch exists
    function getBatch(
        uint256 batchId
    ) external view returns (bytes32 merkleRoot, uint256 txCount, uint256 timestamp) {
        SettlementBatch memory b = batches[batchId];
        return (b.merkleRoot, b.txCount, b.timestamp);
    }

    /// @notice Get contract's token balance (should equal totalDeposited - totalWithdrawn)
    function bridgeBalance() external view returns (uint256) {
        return shlToken.balanceOf(address(this));
    }
}
