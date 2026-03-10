import "dotenv/config";
import "@nomicfoundation/hardhat-ethers";

const PRIVATE_KEY = process.env.DEPLOYER_PRIVATE_KEY || "0x" + "00".repeat(32);
const BASE_SEPOLIA_RPC = process.env.BASE_SEPOLIA_RPC_URL || "https://sepolia.base.org";
const BASE_MAINNET_RPC = process.env.BASE_MAINNET_RPC_URL || "https://mainnet.base.org";

/** @type import('hardhat/config').HardhatUserConfig */
export default {
  solidity: {
    version: "0.8.24",
    settings: { optimizer: { enabled: true, runs: 200 } },
  },
  networks: {
    base_sepolia: {
      type: "http",
      url: BASE_SEPOLIA_RPC,
      accounts: [PRIVATE_KEY],
      chainId: 84532,
    },
    base: {
      type: "http",
      url: BASE_MAINNET_RPC,
      accounts: [PRIVATE_KEY],
      chainId: 8453,
    },
  },
};
