# MEV Arbitrage Bot

Production-grade MEV flash loan arbitrage bot using Aave V3, Uniswap V2/V3, and Sushiswap.
Detects cross-DEX price discrepancies, executes atomic flash loan arbitrage, and profits from
the spread — all in a single Ethereum transaction.

---

## How MEV and Flash Loan Arbitrage Works

### What is MEV?

**Miner Extractable Value (MEV)** — now called **Maximal Extractable Value** — is profit extracted
from Ethereum users by reordering, inserting, or censoring transactions within a block. Arbitrage
bots are one of the most common forms of MEV: they detect price differences between DEXes and
profitably close the gap.

### What is a Flash Loan?

A **flash loan** is an uncollateralized loan that must be borrowed and repaid within the same
Ethereum transaction. If repayment fails, the entire transaction reverts as if it never happened.
Aave V3 charges **0.05%** (5 bps) on flash loans.

This means:
- You can borrow $1,000,000 USDC with $0 capital
- Execute a complex trade
- Repay $1,000.50 USDC
- Keep the rest as profit
- If you can't repay, the tx reverts and no funds are lost

### The Arbitrage Opportunity

DEXes use **Automated Market Makers (AMMs)** based on the constant-product formula:

```
x * y = k
```

Where `x` and `y` are token reserves and `k` is constant. When a large trade happens on one
DEX, its price shifts. Until arbitrageurs close the gap, a price difference exists:

```
Uniswap V2:  1 ETH = 2,500 USDC    (cheaper ETH)
Sushiswap:   1 ETH = 2,515 USDC    (more expensive ETH)
Spread:      15 USDC / ETH = 0.60%
```

The arbitrage:
1. Borrow 100,000 USDC from Aave (flash loan)
2. Buy ETH on Uniswap V2:  100,000 USDC -> 39.98 ETH
3. Sell ETH on Sushiswap:  39.98 ETH -> 100,549 USDC
4. Repay Aave:             100,050 USDC (principal + 0.05% fee)
5. Profit:                 499 USDC (~$499 in one transaction)

---

## Architecture

```
                         +------------------+
                         |   Ethereum Node  |
                         |  (WebSocket RPC) |
                         +--------+---------+
                                  |
                     newPendingTransactions
                                  |
                    +-------------v-----------+
                    |    MempoolScanner        |
                    |  - Filter DEX swaps      |
                    |  - Decode calldata       |
                    |  - Detect large trades   |
                    +-------------+-----------+
                                  |  SwapEvent queue
                    +-------------v-----------+
                    |  OpportunityDetector     |
                    |  - Poll DEX reserves     |
                    |  - Calculate spreads     |
                    |  - Optimal loan amount   |
                    |  - Gas cost vs profit    |
                    +-------------+-----------+
                                  |  ArbOpportunity queue
                    +-------------v-----------+
                    |       Executor           |
                    |  - Simulate on-chain     |
                    |  - Build EIP-1559 tx     |
                    |  - Sign + broadcast      |
                    |  - Monitor confirmation  |
                    |  - Circuit breaker       |
                    +-------------+-----------+
                                  |
                    +-------------v-----------+
                    |   FlashLoanArb.sol       |
                    |   (On-chain contract)    |
                    +---+---------------------+
                        |
           +------------+------------+
           |                         |
    +------v------+          +-------v------+
    | Aave V3     |          | ArbExecutor  |
    | Flash Loan  |          | - Swap on    |
    | Pool        |          |   Uni V2/V3  |
    +------+------+          |   Sushiswap  |
           |                 +-------+------+
           | Borrow/Repay            |
           +<------------------------+
              Single atomic tx
```

---

## Real Arbitrage Example (With Math)

**Setup:** USDC/WETH pair, block #19,854,302

**On-chain reserves:**
```
Uniswap V2 USDC/WETH pool:
  reserveUSDC = 45,230,481 USDC
  reserveWETH = 18,200.42 WETH
  Price: 45,230,481 / 18,200.42 = 2,485.14 USDC/WETH

Sushiswap USDC/WETH pool:
  reserveUSDC = 12,847,203 USDC
  reserveWETH = 5,145.89 WETH
  Price: 12,847,203 / 5,145.89 = 2,496.63 USDC/WETH

Spread: (2496.63 - 2485.14) / 2485.14 = 0.462%
```

**Fee breakdown:**
```
Aave flash loan fee:  0.05%
Uniswap V2 swap fee:  0.30%
Sushiswap swap fee:   0.30%
Total fees:           0.65%
```

**Is this profitable?** Spread (0.462%) < Total fees (0.65%) = NOT profitable.
The bot will skip this and wait for a larger spread.

**When it IS profitable (spread = 0.80%):**
```
Optimal loan amount = sqrt(reserve_uni * reserve_sushi) - reserve_uni
                    = sqrt(45,230,481 * 12,847,203) - 45,230,481
                    = sqrt(581,078,419,573,443) - 45,230,481
                    = 24,105,777 - 45,230,481
                    (negative = we need a different formula for this direction)

For a 0.8% spread with 50,000 USDC loan:
  Buy 20.04 WETH on Uniswap V2  @ 2,495 USDC/WETH
  Sell 20.04 WETH on Sushiswap  @ 2,514.96 USDC/WETH
  
  USDC spent:    50,000.00
  USDC returned: 50,399.74 (= 20.04 * 2,514.96)
  Aave fee:          25.00 (0.05% of 50,000)
  Swap fees:        ~300.00 (0.3% * 2 hops)
  NET PROFIT:        74.74 USDC
```

---

## Project Structure

```
mev-arb-bot/
├── contracts/
│   ├── FlashLoanArb.sol          # Core flash loan + arb contract
│   ├── ArbExecutor.sol           # Multi-DEX swap router (inherited)
│   ├── interfaces/
│   │   ├── IFlashLoanReceiver.sol # Aave V3 receiver interface
│   │   └── IAavePool.sol          # Aave V3 pool interface
│   └── test/
│       ├── MockERC20.sol          # Mintable token for tests
│       ├── MockV2Router.sol       # Configurable mock router
│       └── MockAavePool.sol       # Mock Aave pool for unit tests
├── scripts/
│   ├── deploy.js                 # Deploy to Sepolia + verify on Etherscan
│   └── simulate-arb.js           # Mainnet fork simulation
├── test/
│   └── FlashLoanArb.test.js      # Comprehensive test suite (12 test groups)
├── bot/
│   ├── main.py                   # Async orchestrator + config loader
│   ├── mempool_scanner.py        # WebSocket pending tx subscriber
│   ├── opportunity_detector.py   # Cross-DEX price monitor + arb calculator
│   ├── executor.py               # Tx builder + signer + circuit breaker
│   ├── gas_optimizer.py          # EIP-1559 gas oracle + history
│   └── dashboard.py              # Rich terminal live dashboard
├── hardhat.config.js             # Hardhat config (Sepolia + mainnet fork)
├── package.json
├── requirements.txt
├── .env.example                  # Environment variable template
└── README.md
```

---

## Setup Instructions

### Prerequisites

- Node.js >= 18
- Python >= 3.10
- An Alchemy or Infura account (for RPC endpoints)
- A wallet with Sepolia ETH for deployment

### 1. Clone and Install

```bash
git clone <your-repo>
cd mev-arb-bot

# Install Node dependencies
npm install

# Install Python dependencies
cd bot
pip install -r ../requirements.txt
cd ..
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `PRIVATE_KEY` — your wallet private key (0x prefixed)
- `SEPOLIA_RPC_URL` — Alchemy/Infura Sepolia HTTP endpoint
- `MAINNET_RPC_URL` — Mainnet HTTP endpoint (for fork simulation)
- `WEBSOCKET_URL` — Mainnet WebSocket endpoint (for mempool scanning)
- `ETHERSCAN_API_KEY` — for contract verification

### 3. Compile Contracts

```bash
npm run compile
```

Expected output:
```
Compiled 9 Solidity files successfully
```

### 4. Run Tests

```bash
npm test
```

Expected output:
```
  FlashLoanArb
    1. Deployment & Initialization
      5 passing
    2. Access Control (onlyOwner)
      5 passing
    3. Flash Loan Callback Security
      2 passing
    ...
    
  ArbExecutor
    ...

  47 passing (8s)
```

---

## Deployment Guide

### Deploy to Sepolia Testnet

```bash
npm run deploy:sepolia
```

Output:
```
========================================
  MEV Arb Bot - Deployment Script
========================================

Network:         sepolia
Deployer:        0xYourAddress
Balance:         0.500000 ETH

Aave V3 Pool:    0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951
Addr Provider:   0x012bAC54348C0E635dCAc9D5FB99f06F24136C9A

[1/4] Checking compilation artifacts...
      OK - FlashLoanArb artifact found

[2/4] Deploying FlashLoanArb...
      Deployed at: 0xYourContractAddress
      Tx hash:     0x...

[3/4] Funding contract with ETH for gas...
      Sent 0.01 ETH to contract

[4/4] Verifying on Etherscan...
      Contract verified!
```

After deployment, copy the contract address to your `.env`:
```
FLASHLOAN_CONTRACT_ADDRESS=0xYourContractAddress
```

### Run Mainnet Fork Simulation

```bash
# Terminal 1: start fork node
npx hardhat node --fork $MAINNET_RPC_URL

# Terminal 2: run simulation
npm run simulate
```

---

## Running the Bot

```bash
cd bot
python main.py
```

The Rich terminal dashboard will launch showing:
- Real-time mempool scanning stats
- Current DEX price spreads
- Gas prices (base fee, priority, estimated arb cost)
- Execution history with profit/loss
- Circuit breaker status

### Running in Dry Run Mode

If `PRIVATE_KEY` or `FLASHLOAN_CONTRACT_ADDRESS` is not set, the bot automatically
runs in **DRY RUN mode** — it detects opportunities and simulates executions without
sending any real transactions. Useful for testing your RPC setup and validating
the opportunity detection logic.

---

## How the Bot Detects Opportunities

### 1. Mempool Monitoring (mempool_scanner.py)

The scanner subscribes to `eth_subscribe("newPendingTransactions")` via WebSocket.
For every pending transaction, it:

1. Checks if `tx.to` is a known DEX router (Uniswap V2/V3, Sushiswap)
2. Decodes the function selector from `tx.input` (first 4 bytes)
3. ABI-decodes the calldata to extract token pairs and amounts
4. Pushes a `SwapEvent` to the detector's queue

**Why watch pending txs?**
Large trades move the price significantly. If someone is about to swap $2M USDC for WETH
on Uniswap, the price will shift by ~0.4%. The bot can detect this BEFORE it's mined and
position itself to capture the arb.

### 2. Price Polling (opportunity_detector.py)

In parallel, the detector polls `getReserves()` on V2 pair contracts every second for
all watched pairs. This catches price discrepancies that weren't triggered by a specific
pending transaction.

**Opportunity filtering:**
```python
# Only consider if spread covers all fees with margin
MIN_SPREAD_BPS = 70   # 0.70% minimum (fees = 0.65%)

# Calculate optimal loan amount using closed-form solution:
# optimal = sqrt(reserve_a * reserve_b) - reserve_a

# Estimate profit using constant-product formula with fees:
# out = (amountIn * 9970 * reserveOut) / (reserveIn * 10000 + amountIn * 9970)

# Only execute if:
# profit_usd > MIN_PROFIT_USD ($10 default)
# gas_cost_usd < 80% of expected profit
```

### 3. On-Chain Simulation (executor.py)

Before submitting a transaction, the executor calls `simulateArb()` — a `view` function
on the contract — to get a final on-chain profit estimate. This catches price changes
between detection and execution.

---

## Contract Addresses

### Sepolia Testnet

| Contract          | Address                                      |
|-------------------|----------------------------------------------|
| FlashLoanArb      | Deploy with `npm run deploy:sepolia`          |
| Aave V3 Pool      | `0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951` |
| Aave Addr Provider| `0x012bAC54348C0E635dCAc9D5FB99f06F24136C9A` |

### Mainnet References

| Contract          | Address                                      |
|-------------------|----------------------------------------------|
| Aave V3 Pool      | `0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2` |
| Uniswap V2 Router | `0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D` |
| Uniswap V3 Router | `0xE592427A0AEce92De3Edee1F18E0157C05861564` |
| Sushiswap Router  | `0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F` |
| Uniswap V2 Factory| `0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f` |
| Sushiswap Factory | `0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac` |

---

## Performance Stats

### Expected Gas Usage

| Operation             | Gas Units |
|-----------------------|-----------|
| Flash loan borrow     | ~80,000   |
| V2 swap (buy)         | ~110,000  |
| V2 swap (sell)        | ~110,000  |
| Approve + repay       | ~60,000   |
| Contract overhead     | ~40,000   |
| **Total**             | **~400,000** |

At 30 Gwei base fee: ~0.012 ETH (~$30 at $2,500/ETH)

### Minimum Profitable Spread

For a $50,000 flash loan at 30 Gwei gas:
```
Fees:           $50,000 * 0.65% = $325
Gas:            ~$30
Total overhead: ~$355
Min profit:     ~0.71% spread on $50,000 = $355
```

For larger loans, the gas cost is amortized over a bigger position, improving efficiency.

### Realistic Win Rate

- On mainnet with competition: 20-40% success rate on submitted transactions
- Most arbs are won by highly optimized bots with direct miner relationships
- Sepolia testnet: ~85% success rate (no real competition)

---

## Risk Warnings

> **WARNING: This software is for educational and research purposes.
> Using this on mainnet carries significant financial risk.**

### Technical Risks

1. **Sandwich attacks** — Your transaction can be front-run and sandwiched, causing the
   arb to be unprofitable. The `minProfit` parameter protects against this but cannot
   guarantee it.

2. **Race conditions** — Many bots compete for the same opportunities. Your transaction
   may be submitted but lose the race, costing gas with no profit.

3. **Smart contract bugs** — Despite testing, bugs can result in loss of funds.
   Audit the contracts before deploying significant capital.

4. **Gas price spikes** — During network congestion, gas costs can exceed the profit
   margin. The `MAX_GAS_GWEI` setting provides protection but may miss opportunities.

5. **Flash loan reversion** — If the arb is no longer profitable when mined (price
   moved), the transaction reverts. You lose the gas cost (~0.012 ETH).

### Financial Risks

1. **MEV competition is intense** — Flashbots, proprietary searchers, and large
   institutional bots dominate the mainnet arb space. Do not expect consistent profits
   without significant competitive advantage.

2. **Capital at risk** — While flash loans require no collateral, the contract must
   hold ETH for gas. This ETH is at risk if the contract has bugs.

3. **Regulatory risk** — MEV extraction is in a legal grey area in some jurisdictions.
   Consult a lawyer before operating commercially.

### Operational Risks

1. **RPC reliability** — Public RPCs are unreliable. Use a dedicated node or
   paid service (Alchemy, Infura) with WebSocket support.

2. **Private key security** — Never commit your `.env` file. Use a hardware wallet
   for production deployments.

3. **Circuit breaker** — The circuit breaker stops the bot after 3 consecutive losses.
   This is a safety feature — do not disable it without understanding why trades failed.

---

## Configuration Reference

| Variable                      | Default    | Description                              |
|-------------------------------|------------|------------------------------------------|
| `PRIVATE_KEY`                 | required   | Wallet private key (0x prefix)           |
| `SEPOLIA_RPC_URL`             | required   | Sepolia HTTP RPC endpoint                |
| `MAINNET_RPC_URL`             | required   | Mainnet HTTP RPC endpoint                |
| `WEBSOCKET_URL`               | required   | Mainnet WebSocket endpoint               |
| `FLASHLOAN_CONTRACT_ADDRESS`  | required   | Deployed FlashLoanArb address            |
| `MIN_PROFIT_USD`              | `10`       | Minimum profit threshold in USD          |
| `MAX_GAS_GWEI`                | `50`       | Maximum gas price to execute at          |
| `MIN_PROFIT_BPS`              | `30`       | Minimum profit in basis points           |
| `MAX_LOAN_USD`                | `100000`   | Maximum flash loan size in USD           |
| `MAX_CONSECUTIVE_LOSSES`      | `3`        | Circuit breaker trigger                  |
| `DASHBOARD_REFRESH`           | `2`        | Dashboard refresh interval (seconds)     |
| `LOG_LEVEL`                   | `INFO`     | Logging verbosity                        |
| `LOG_FILE`                    | (stdout)   | Log file path                            |

---

## Development

### Adding a New DEX

1. Add the router address to `.env.example` and `main.py` Config class
2. Add the router to `MempoolScanner.dex_routers` dict
3. Add factory to `OpportunityDetector` and implement price fetch
4. Add a new `DexType` enum value in `ArbExecutor.sol` if needed
5. Implement `_executeSwap` routing for the new DEX type

### Adding a New Token Pair

In `opportunity_detector.py`, add to `WATCHED_PAIRS`:
```python
WATCHED_PAIRS = [
    ...
    ("0xTokenA_Address", "0xTokenB_Address"),
]
```

Add decimals and USD price to `TOKEN_DECIMALS` and `TOKEN_USD_PRICES`.

### Running Tests with Coverage

```bash
npm run test:coverage
```

### Gas Report

```bash
REPORT_GAS=true npm test
```

---

## License

MIT License. See LICENSE file.

This project is provided for educational purposes. The authors are not responsible
for any financial losses incurred from using this software.
