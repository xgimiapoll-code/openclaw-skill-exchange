// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

/**
 * @title SHLToken — Shell Token (贝壳)
 * @notice ERC-20 token for the Openclaw Skill Exchange ecosystem.
 *         6 decimals to match the off-chain micro-SHL unit (1 SHL = 1,000,000 micro-SHL).
 *         Owner (bridge operator) can mint tokens for deposits from off-chain.
 *         Users can burn tokens to withdraw to off-chain.
 */
contract SHLToken is ERC20, Ownable {
    constructor() ERC20("Shell Token", "SHL") Ownable(msg.sender) {}

    function decimals() public pure override returns (uint8) {
        return 6;
    }

    /// @notice Mint tokens — only bridge operator can call (for deposit from off-chain)
    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }

    /// @notice Burn tokens — anyone can burn their own tokens (for withdraw to off-chain)
    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }
}
