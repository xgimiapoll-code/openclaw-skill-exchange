/**
 * Deploy SHLToken + SHLBridge to Base (Sepolia or Mainnet).
 *
 * Usage:
 *   npx hardhat run scripts/deploy.js --network base_sepolia
 *   npx hardhat run scripts/deploy.js --network base
 */
import hre from "hardhat";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  console.log("Deployer:", deployer.address);
  console.log(
    "Balance:",
    hre.ethers.formatEther(await hre.ethers.provider.getBalance(deployer.address)),
    "ETH"
  );

  // 1. Deploy SHLToken
  console.log("\n--- Deploying SHLToken ---");
  const SHLToken = await hre.ethers.getContractFactory("SHLToken");
  const token = await SHLToken.deploy();
  await token.waitForDeployment();
  const tokenAddr = await token.getAddress();
  console.log("SHLToken deployed at:", tokenAddr);

  // 2. Deploy SHLBridge (pass token address)
  console.log("\n--- Deploying SHLBridge ---");
  const SHLBridge = await hre.ethers.getContractFactory("SHLBridge");
  const bridge = await SHLBridge.deploy(tokenAddr);
  await bridge.waitForDeployment();
  const bridgeAddr = await bridge.getAddress();
  console.log("SHLBridge deployed at:", bridgeAddr);

  // 3. Mint initial supply to bridge (100,000 SHL = 100_000_000_000 micro-SHL)
  const initialSupply = 100_000n * 1_000_000n; // 100k SHL in 6 decimals
  console.log("\n--- Minting initial supply ---");
  const mintTx = await token.mint(bridgeAddr, initialSupply);
  await mintTx.wait();
  console.log(`Minted ${Number(initialSupply) / 1_000_000} SHL to bridge`);

  // 4. Verify bridge balance
  const bridgeBal = await token.balanceOf(bridgeAddr);
  console.log("Bridge token balance:", Number(bridgeBal) / 1_000_000, "SHL");

  // 5. Save deployment info
  const deployment = {
    network: hre.network.name,
    chainId: (await hre.ethers.provider.getNetwork()).chainId.toString(),
    deployer: deployer.address,
    contracts: {
      SHLToken: tokenAddr,
      SHLBridge: bridgeAddr,
    },
    initialSupply: initialSupply.toString(),
    deployedAt: new Date().toISOString(),
  };

  const outDir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(outDir, { recursive: true });
  const outFile = path.join(outDir, `${hre.network.name}.json`);
  fs.writeFileSync(outFile, JSON.stringify(deployment, null, 2));
  console.log(`\nDeployment info saved to: ${outFile}`);

  // 6. Print env vars for backend
  console.log("\n=== Add to .env ===");
  console.log(`SHL_TOKEN_ADDRESS=${tokenAddr}`);
  console.log(`SHL_BRIDGE_ADDRESS=${bridgeAddr}`);
  console.log(`BRIDGE_OPERATOR_ADDRESS=${deployer.address}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
