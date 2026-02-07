/**
 * deploy.js
 * Deploy FlashLoanArb to Sepolia testnet
 *
 * Usage:
 *   npx hardhat run scripts/deploy.js --network sepolia
 *
 * Prerequisites:
 *   - PRIVATE_KEY set in .env (wallet with >= 0.1 SepoliaETH)
 *   - SEPOLIA_RPC_URL set in .env
 *   - ETHERSCAN_API_KEY set in .env (for verification)
 */

const { ethers, run, network } = require("hardhat");
require("dotenv").config();

// ─────────────────────────────────────────────────────────────────────────────
// Network-specific Aave V3 addresses
// ─────────────────────────────────────────────────────────────────────────────
const AAVE_ADDRESSES = {
  sepolia: {
    pool:              "0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951",
    addressesProvider: "0x012bAC54348C0E635dCAc9D5FB99f06F24136C9A",
  },
  mainnet: {
    pool:              "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
    addressesProvider: "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9E",
  },
  hardhat: {
    // Forked mainnet — use mainnet addresses
    pool:              "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
    addressesProvider: "0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9E",
  },
};

// ─────────────────────────────────────────────────────────────────────────────
// Deployment amount to send to contract for gas buffer
// ─────────────────────────────────────────────────────────────────────────────
const ETH_TO_FUND = ethers.parseEther("0.01"); // 0.01 ETH for gas

async function main() {
  console.log("\n========================================");
  console.log("  MEV Arb Bot — Deployment Script");
  console.log("========================================\n");

  // ── Get signer ─────────────────────────────────────────────────────────────
  const [deployer] = await ethers.getSigners();
  const deployerAddress = await deployer.getAddress();
  const balance = await ethers.provider.getBalance(deployerAddress);

  console.log(`Network:         ${network.name}`);
  console.log(`Deployer:        ${deployerAddress}`);
  console.log(`Balance:         ${ethers.formatEther(balance)} ETH`);

  if (balance < ETH_TO_FUND + ethers.parseEther("0.005")) {
    console.warn("\n[WARN] Low balance — you may not have enough ETH for deployment + funding");
  }

  // ── Resolve Aave addresses ──────────────────────────────────────────────────
  const aaveConfig = AAVE_ADDRESSES[network.name];
  if (!aaveConfig) {
    throw new Error(`No Aave config for network: ${network.name}`);
  }

  console.log(`\nAave V3 Pool:    ${aaveConfig.pool}`);
  console.log(`Addr Provider:   ${aaveConfig.addressesProvider}`);

  // ── Compile check ──────────────────────────────────────────────────────────
  console.log("\n[1/4] Checking compilation artifacts...");
  const FlashLoanArb = await ethers.getContractFactory("FlashLoanArb");
  console.log("      OK — FlashLoanArb artifact found");

  // ── Deploy ──────────────────────────────────────────────────────────────────
  console.log("\n[2/4] Deploying FlashLoanArb...");

  const constructorArgs = [
    aaveConfig.pool,
    aaveConfig.addressesProvider,
    deployerAddress, // owner = deployer
  ];

  const flashLoanArb = await FlashLoanArb.deploy(...constructorArgs);
  await flashLoanArb.waitForDeployment();

  const contractAddress = await flashLoanArb.getAddress();
  console.log(`      Deployed at: ${contractAddress}`);
  console.log(`      Tx hash:     ${flashLoanArb.deploymentTransaction().hash}`);

  // ── Fund with small ETH for gas ─────────────────────────────────────────────
  console.log("\n[3/4] Funding contract with ETH for gas...");
  const fundTx = await deployer.sendTransaction({
    to: contractAddress,
    value: ETH_TO_FUND,
  });
  await fundTx.wait();
  console.log(`      Sent ${ethers.formatEther(ETH_TO_FUND)} ETH to contract`);
  console.log(`      Fund tx: ${fundTx.hash}`);

  // ── Etherscan verification ─────────────────────────────────────────────────
  if (network.name !== "hardhat" && network.name !== "localhost") {
    console.log("\n[4/4] Verifying on Etherscan (waiting 30s for indexing)...");
    await sleep(30000);

    try {
      await run("verify:verify", {
        address: contractAddress,
        constructorArguments: constructorArgs,
        contract: "contracts/FlashLoanArb.sol:FlashLoanArb",
      });
      console.log("      Contract verified on Etherscan!");
    } catch (err) {
      if (err.message.includes("Already Verified")) {
        console.log("      Already verified.");
      } else {
        console.error("      Verification failed:", err.message);
        console.log("      You can verify manually with:");
        console.log(
          `      npx hardhat verify --network ${network.name} ${contractAddress} ${constructorArgs.join(" ")}`
        );
      }
    }
  } else {
    console.log("\n[4/4] Skipping Etherscan verification (local network)");
  }

  // ── Summary ────────────────────────────────────────────────────────────────
  const contractEthBalance = await ethers.provider.getBalance(contractAddress);
  const flashLoanPremium = await flashLoanArb.getFlashLoanPremium(ethers.parseUnits("1000", 6)); // 1000 USDC

  console.log("\n========================================");
  console.log("  Deployment Summary");
  console.log("========================================");
  console.log(`Contract address:    ${contractAddress}`);
  console.log(`Network:             ${network.name}`);
  console.log(`Aave V3 Pool:        ${aaveConfig.pool}`);
  console.log(`Owner:               ${deployerAddress}`);
  console.log(`Contract ETH bal:    ${ethers.formatEther(contractEthBalance)} ETH`);
  console.log(`Flash loan fee:      ${flashLoanPremium} for 1000 USDC borrow`);
  console.log(`Etherscan URL:       https://${network.name === "sepolia" ? "sepolia." : ""}etherscan.io/address/${contractAddress}`);

  console.log("\n========================================");
  console.log("  Next Steps");
  console.log("========================================");
  console.log("1. Copy contract address to your .env:");
  console.log(`   FLASHLOAN_CONTRACT_ADDRESS=${contractAddress}`);
  console.log("2. Pre-approve DEX routers to save gas:");
  console.log("   npx hardhat run scripts/approve-routers.js --network sepolia");
  console.log("3. Start the bot:");
  console.log("   cd bot && python main.py");

  // Write deployment info to file for the bot to consume
  const fs = require("fs");
  const deploymentInfo = {
    network: network.name,
    contractAddress,
    aavePool: aaveConfig.pool,
    addressesProvider: aaveConfig.addressesProvider,
    owner: deployerAddress,
    deployTxHash: flashLoanArb.deploymentTransaction().hash,
    deployedAt: new Date().toISOString(),
  };

  const outPath = `deployments/${network.name}.json`;
  fs.mkdirSync("deployments", { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(deploymentInfo, null, 2));
  console.log(`\nDeployment info saved to ${outPath}`);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error("\n[ERROR] Deployment failed:", err);
    process.exit(1);
  });
