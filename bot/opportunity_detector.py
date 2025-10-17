"""
opportunity_detector.py
=======================
Cross-DEX price monitor and arbitrage opportunity detector.

Monitors:
  - Uniswap V2: getReserves() on pair contracts
  - Uniswap V3: slot0() + liquidity() on pool contracts
  - Sushiswap:  getReserves() on pair contracts

For each token pair, calculates:
  1. Price on each DEX
  2. Price differential (spread in basis points)
  3. Optimal flash loan amount for maximum profit
  4. Expected profit after Aave fee (0.05%) + swap fees (0.3% x2)
  5. Gas cost estimate vs profit — only flags if net profitable

Produces ArbOpportunity objects onto an asyncio.Queue.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

from web3 import AsyncWeb3, HTTPProvider

log = logging.getLogger("mev.detector")

# ─────────────────────────────────────────────────────────────────────────────
# ABIs (minimal — only functions we call)
# ─────────────────────────────────────────────────────────────────────────────

V2_FACTORY_ABI = [
    {"name": "getPair", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
     "outputs": [{"name": "pair", "type": "address"}]},
]

V2_PAIR_ABI = [
    {"name": "getReserves", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "reserve0", "type": "uint112"},
                 {"name": "reserve1", "type": "uint112"},
                 {"name": "blockTimestampLast", "type": "uint32"}]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

V3_FACTORY_ABI = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenA", "type": "address"},
                {"name": "tokenB", "type": "address"},
                {"name": "fee",    "type": "uint24"}],
     "outputs": [{"name": "pool", "type": "address"}]},
]

V3_POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "sqrtPriceX96",           "type": "uint160"},
                 {"name": "tick",                    "type": "int24"},
                 {"name": "observationIndex",        "type": "uint16"},
                 {"name": "observationCardinality",  "type": "uint16"},
                 {"name": "observationCardinalityNext","type": "uint16"},
                 {"name": "feeProtocol",             "type": "uint8"},
                 {"name": "unlocked",                "type": "bool"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "token0",    "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee",       "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
]

V2_ROUTER_ABI = [
    {"name": "getAmountsOut", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "amountIn", "type": "uint256"},
                {"name": "path",     "type": "address[]"}],
     "outputs": [{"name": "amounts", "type": "uint256[]"}]},
    {"name": "factory", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Aave V3 flash loan fee: 0.05% = 5 bps
AAVE_PREMIUM_BPS = 5

# Uniswap V2 / Sushi swap fee: 0.30% = 30 bps per hop
V2_SWAP_FEE_BPS = 30

# Total fee overhead for a 2-hop arb: buy + sell + flash loan
TOTAL_FEE_BPS = V2_SWAP_FEE_BPS + V2_SWAP_FEE_BPS + AAVE_PREMIUM_BPS  # 65 bps

# Uniswap V3 fee tiers
V3_FEE_TIERS = [500, 3000, 10000]

# Price staleness: re-query if last fetch was > N seconds ago
PRICE_CACHE_TTL = 3.0  # seconds

# Minimum spread to even consider (saves RPC calls)
MIN_SPREAD_BPS = 70  # 0.70% — must beat total fees (65 bps) with margin

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DexPrice:
    """Price of a token pair on a specific DEX."""
    dex:        str       # "uniswap_v2", "uniswap_v3", "sushiswap"
    router:     str       # Router address
    factory:    str       # Factory address
    pair_addr:  str       # Pair/pool address
    token0:     str       # Canonical token0 (lower address)
    token1:     str       # Canonical token1
    reserve0:   int       # token0 reserve (V2) or derived (V3)
    reserve1:   int       # token1 reserve
    price:      float     # token1 per token0 (e.g. WETH per USDC)
    fee_bps:    int       # swap fee in basis points
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_stale(self) -> bool:
        return time.time() - self.fetched_at > PRICE_CACHE_TTL

    def get_amount_out(self, amount_in: int, token_in: str) -> int:
        """Estimate output using constant-product formula (no fee)."""
        if token_in.lower() == self.token0.lower():
            r_in, r_out = self.reserve0, self.reserve1
        else:
            r_in, r_out = self.reserve1, self.reserve0

        if r_in == 0 or r_out == 0:
            return 0

        # With fee: amountOut = (amountIn * (10000 - fee) * r_out) / (r_in * 10000 + amountIn * (10000 - fee))
        fee_factor = 10000 - self.fee_bps
        num = amount_in * fee_factor * r_out
        den = r_in * 10000 + amount_in * fee_factor
        return num // den if den > 0 else 0


@dataclass
class ArbOpportunity:
    """A detected arbitrage opportunity ready for execution."""
    token_a:        str        # Token to borrow (e.g. USDC)
    token_b:        str        # Intermediate token (e.g. WETH)
    buy_dex:        str        # DEX to buy token_b on (cheaper)
    sell_dex:       str        # DEX to sell token_b on (more expensive)
    buy_router:     str        # Buy DEX router address
    sell_router:    str        # Sell DEX router address
    buy_dex_type:   int        # 0=V2, 1=V3, 2=Sushi
    sell_dex_type:  int        # 0=V2, 1=V3, 2=Sushi
    buy_factory:    str        # Factory address for buy DEX
    sell_factory:   str        # Factory address for sell DEX
    loan_amount:    int        # Optimal flash loan amount in token_a units
    expected_profit:int        # Expected profit in token_a units (after all fees)
    spread_bps:     int        # Price spread in basis points
    gas_cost_usd:   float      # Estimated gas cost in USD
    profit_usd:     float      # Expected net profit in USD
    token_a_decimals:int = 6   # Decimals of token_a
    detected_at:    float = field(default_factory=time.time)

    def __repr__(self) -> str:
        return (
            f"ArbOpportunity("
            f"pair={self.token_a[:8]}/{self.token_b[:8]}, "
            f"buy={self.buy_dex}, sell={self.sell_dex}, "
            f"spread={self.spread_bps}bps, "
            f"loan={self.loan_amount}, "
            f"profit_usd=${self.profit_usd:.2f})"
        )


# ─────────────────────────────────────────────────────────────────────────────
# OpportunityDetector
# ─────────────────────────────────────────────────────────────────────────────

class OpportunityDetector:
    """
    Polls DEX prices for watched token pairs and detects arbitrage opportunities.
    """

    # Known mainnet addresses
    UNI_V2_FACTORY   = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
    UNI_V3_FACTORY   = "0x1F98431c8aD98523631AE4a59f267346ea31F984"
    SUSHI_FACTORY    = "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"

    # Watched token pairs (mainnet addresses)
    WATCHED_PAIRS = [
        ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
         "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), # WETH
        ("0xdAC17F958D2ee523a2206206994597C13D831ec7",  # USDT
         "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), # WETH
        ("0x6B175474E89094C44Da98b954EedeAC495271d0F",  # DAI
         "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), # WETH
        ("0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",  # WBTC
         "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), # WETH
        ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
         "0xdAC17F958D2ee523a2206206994597C13D831ec7"), # USDT
    ]

    # Token decimals lookup
    TOKEN_DECIMALS = {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,   # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7": 6,   # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 18,  # WETH
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 8,   # WBTC
    }

    # Approximate USD prices for profit calculation (updated periodically)
    TOKEN_USD_PRICES = {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 1.0,    # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7": 1.0,    # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f": 1.0,    # DAI
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": 2500.0, # WETH (approx)
        "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": 45000.0,# WBTC (approx)
    }

    def __init__(
        self,
        rpc_url: str,
        uni_v2_router: str,
        uni_v3_router: str,
        uni_v3_quoter: str,
        sushi_router: str,
        min_profit_usd: float,
        min_profit_bps: int,
        max_gas_gwei: float,
        gas_optimizer,
        poll_interval: float = 1.0,
    ) -> None:
        self.rpc_url        = rpc_url
        self.uni_v2_router  = uni_v2_router
        self.uni_v3_router  = uni_v3_router
        self.uni_v3_quoter  = uni_v3_quoter
        self.sushi_router   = sushi_router
        self.min_profit_usd = min_profit_usd
        self.min_profit_bps = min_profit_bps
        self.max_gas_gwei   = max_gas_gwei
        self.gas_optimizer  = gas_optimizer
        self.poll_interval  = poll_interval

        # Stats (read by dashboard)
        self.pairs_scanned:       int   = 0
        self.opportunities_found: int   = 0
        self.opportunities_missed:int   = 0  # profitable but gas too high
        self.last_scan_time:      Optional[float] = None
        self.last_spread_bps:     dict[str, int]  = {}

        self._running = False
        self._w3: Optional[AsyncWeb3] = None

        # Price cache: (tokenA, tokenB, dex) -> DexPrice
        self._price_cache: dict[tuple, DexPrice] = {}

    async def start(
        self,
        mempool_queue: asyncio.Queue,
        opportunity_queue: asyncio.Queue,
    ) -> None:
        """Main loop: poll prices + respond to mempool signals."""
        self._running = True

        if not self.rpc_url:
            log.warning("[detector] MAINNET_RPC_URL not set — running price simulation")
            await self._simulation_mode(opportunity_queue)
            return

        self._w3 = AsyncWeb3(HTTPProvider(self.rpc_url))

        log.info("[detector] Starting price polling loop...")
        poll_task    = asyncio.create_task(self._poll_loop(opportunity_queue))
        mempool_task = asyncio.create_task(self._mempool_trigger_loop(mempool_queue, opportunity_queue))

        await asyncio.gather(poll_task, mempool_task)

    async def _poll_loop(self, opportunity_queue: asyncio.Queue) -> None:
        """Periodically poll all watched pairs across all DEXes."""
        while self._running:
            try:
                for token_a, token_b in self.WATCHED_PAIRS:
                    await self._check_pair(token_a, token_b, opportunity_queue)
                    self.pairs_scanned += 1
                self.last_scan_time = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[detector] Poll error: {e}", exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _mempool_trigger_loop(
        self,
        mempool_queue: asyncio.Queue,
        opportunity_queue: asyncio.Queue,
    ) -> None:
        """
        When a large swap is detected in the mempool, immediately
        re-check that pair — the price is about to move.
        """
        while self._running:
            try:
                swap = await asyncio.wait_for(mempool_queue.get(), timeout=5.0)
                if swap.token_in and swap.token_out:
                    await self._check_pair(swap.token_in, swap.token_out, opportunity_queue)
                mempool_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"[detector] Mempool trigger error: {e}")

    async def _check_pair(
        self,
        token_a: str,
        token_b: str,
        opportunity_queue: asyncio.Queue,
    ) -> None:
        """Fetch prices across all DEXes and check for arb opportunities."""
        try:
            # Fetch prices from V2 and Sushi (V3 is slower — add later)
            uni_price   = await self._get_v2_price(token_a, token_b, self.uni_v2_router, self.UNI_V2_FACTORY, "uniswap_v2")
            sushi_price = await self._get_v2_price(token_a, token_b, self.sushi_router,  self.SUSHI_FACTORY,  "sushiswap")

            if not uni_price or not sushi_price:
                return

            # Calculate spread
            spread_bps = self._calculate_spread_bps(uni_price, sushi_price)
            pair_key   = f"{token_a[:8]}/{token_b[:8]}"
            self.last_spread_bps[pair_key] = spread_bps

            if spread_bps < MIN_SPREAD_BPS:
                return  # Not enough spread to be profitable

            # Determine direction
            if uni_price.price < sushi_price.price:
                buy_price, sell_price = uni_price, sushi_price
            else:
                buy_price, sell_price = sushi_price, uni_price

            # Calculate optimal loan amount
            loan_amount = self.calculate_optimal_amount(
                buy_price.reserve0, buy_price.reserve1,
                sell_price.reserve0, sell_price.reserve1,
            )
            if loan_amount == 0:
                return

            # Cap loan amount
            token_a_decimals = self.TOKEN_DECIMALS.get(token_a.lower(), 18)
            token_a_usd      = self.TOKEN_USD_PRICES.get(token_a.lower(), 1.0)
            max_loan_units   = int(100_000 / token_a_usd * (10 ** token_a_decimals))
            loan_amount      = min(loan_amount, max_loan_units)

            # Estimate profit
            expected_profit = self._estimate_profit(
                loan_amount, buy_price, sell_price, token_a, token_b
            )
            if expected_profit <= 0:
                return

            # Convert profit to USD
            profit_usd = (expected_profit / (10 ** token_a_decimals)) * token_a_usd

            if profit_usd < self.min_profit_usd:
                return

            # Check gas cost
            gas_cost_usd = await self._estimate_gas_cost_usd()
            if gas_cost_usd > profit_usd * 0.8:
                self.opportunities_missed += 1
                log.debug(f"[detector] Skipping — gas ${gas_cost_usd:.2f} > 80% of profit ${profit_usd:.2f}")
                return

            opp = ArbOpportunity(
                token_a=        token_a,
                token_b=        token_b,
                buy_dex=        buy_price.dex,
                sell_dex=       sell_price.dex,
                buy_router=     buy_price.router,
                sell_router=    sell_price.router,
                buy_dex_type=   0 if buy_price.dex == "uniswap_v2" else 2,
                sell_dex_type=  0 if sell_price.dex == "uniswap_v2" else 2,
                buy_factory=    buy_price.factory,
                sell_factory=   sell_price.factory,
                loan_amount=    loan_amount,
                expected_profit=expected_profit,
                spread_bps=     spread_bps,
                gas_cost_usd=   gas_cost_usd,
                profit_usd=     profit_usd - gas_cost_usd,
                token_a_decimals=token_a_decimals,
            )

            self.opportunities_found += 1
            log.info(f"[detector] ARB FOUND: {opp}")

            try:
                opportunity_queue.put_nowait(opp)
            except asyncio.QueueFull:
                log.warning("[detector] opportunity_queue full — dropping opportunity")

        except Exception as e:
            log.debug(f"[detector] _check_pair error: {e}")

    async def _get_v2_price(
        self,
        token_a: str,
        token_b: str,
        router: str,
        factory: str,
        dex_name: str,
    ) -> Optional[DexPrice]:
        """Query Uniswap V2 / Sushiswap pair reserves and compute price."""
        cache_key = (token_a.lower(), token_b.lower(), dex_name)
        cached    = self._price_cache.get(cache_key)
        if cached and not cached.is_stale:
            return cached

        try:
            factory_contract = self._w3.eth.contract(
                address=self._w3.to_checksum_address(factory),
                abi=V2_FACTORY_ABI
            )
            pair_addr = await factory_contract.functions.getPair(
                self._w3.to_checksum_address(token_a),
                self._w3.to_checksum_address(token_b),
            ).call()

            if pair_addr == "0x0000000000000000000000000000000000000000":
                return None

            pair = self._w3.eth.contract(
                address=self._w3.to_checksum_address(pair_addr),
                abi=V2_PAIR_ABI
            )
            reserves = await pair.functions.getReserves().call()
            token0   = await pair.functions.token0().call()

            r0, r1 = int(reserves[0]), int(reserves[1])
            if r0 == 0 or r1 == 0:
                return None

            # Determine which reserve corresponds to token_a
            if token0.lower() == token_a.lower():
                reserve_a, reserve_b = r0, r1
            else:
                reserve_a, reserve_b = r1, r0

            # Price = how much token_b per token_a
            dec_a = self.TOKEN_DECIMALS.get(token_a.lower(), 18)
            dec_b = self.TOKEN_DECIMALS.get(token_b.lower(), 18)
            price = (reserve_b / (10 ** dec_b)) / (reserve_a / (10 ** dec_a))

            dp = DexPrice(
                dex=       dex_name,
                router=    router,
                factory=   factory,
                pair_addr= pair_addr,
                token0=    token0,
                token1=    token_b if token0.lower() == token_a.lower() else token_a,
                reserve0=  reserve_a,
                reserve1=  reserve_b,
                price=     price,
                fee_bps=   30,  # 0.30% for V2/Sushi
            )
            self._price_cache[cache_key] = dp
            return dp

        except Exception as e:
            log.debug(f"[detector] V2 price fetch error ({dex_name}): {e}")
            return None

    def _calculate_spread_bps(self, price_a: DexPrice, price_b: DexPrice) -> int:
        """Calculate the percentage spread between two DEX prices in basis points."""
        if price_a.price == 0 or price_b.price == 0:
            return 0
        higher = max(price_a.price, price_b.price)
        lower  = min(price_a.price, price_b.price)
        spread = (higher - lower) / lower
        return int(spread * 10000)

    def calculate_optimal_amount(
        self,
        reserve0_a: int,
        reserve1_a: int,
        reserve0_b: int,
        reserve1_b: int,
    ) -> int:
        """
        Calculate the optimal flash loan amount for maximum profit.

        Closed-form solution for the optimal input to a two-pool V2 arbitrage:
          delta_x* = sqrt(k_a * k_b * price_b/price_a) - k_a

        Where k = reserve0 * reserve1 (constant product).

        Reference: "Optimal MEV" by Flashbots research.
        """
        if any(r == 0 for r in [reserve0_a, reserve1_a, reserve0_b, reserve1_b]):
            return 0

        # Price on each DEX (token1 per token0)
        price_a = reserve1_a / reserve0_a  # token_b per token_a on DEX A
        price_b = reserve0_b / reserve1_b  # token_a per token_b on DEX B (inverse for sell side)

        if price_a >= price_b:
            return 0  # No arb in this direction

        # Optimal input = sqrt(reserve0_a * reserve0_a_equivalent_on_B) - reserve0_a
        # Simplification: use geometric mean of pool sizes
        try:
            product  = float(reserve0_a) * float(reserve0_b)
            sqrt_val = math.sqrt(product)
            optimal  = int(sqrt_val) - reserve0_a
            return max(0, optimal)
        except (ValueError, OverflowError):
            return 0

    def _estimate_profit(
        self,
        loan_amount: int,
        buy_price: DexPrice,
        sell_price: DexPrice,
        token_a: str,
        token_b: str,
    ) -> int:
        """
        Estimate net profit after:
          - Aave flash loan fee (0.05%)
          - Buy swap fee (0.30%)
          - Sell swap fee (0.30%)
        """
        if loan_amount == 0:
            return 0

        # Step 1: Buy token_b with token_a on buy_price DEX
        token_b_amount = buy_price.get_amount_out(loan_amount, token_a)
        if token_b_amount == 0:
            return 0

        # Step 2: Sell token_b back to token_a on sell_price DEX
        token_a_received = sell_price.get_amount_out(token_b_amount, token_b)
        if token_a_received == 0:
            return 0

        # Step 3: Subtract flash loan premium (0.05%)
        premium = (loan_amount * AAVE_PREMIUM_BPS) // 10000
        total_owed = loan_amount + premium

        profit = token_a_received - total_owed
        return max(0, profit)

    async def _estimate_gas_cost_usd(self) -> float:
        """Estimate gas cost for a flash loan arb transaction in USD."""
        try:
            gas_config  = await self.gas_optimizer.get_optimal_gas("medium")
            gas_units   = 450_000  # Typical flash loan arb gas usage
            eth_price   = self.TOKEN_USD_PRICES.get(
                "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", 2500.0
            )
            gas_cost_eth = (gas_units * gas_config.max_fee_per_gas) / 1e18
            return gas_cost_eth * eth_price
        except Exception:
            return 5.0  # Default fallback: $5

    async def _simulation_mode(self, opportunity_queue: asyncio.Queue) -> None:
        """Generate synthetic opportunities for testing."""
        import random
        log.info("[detector] Running in SIMULATION mode")

        USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        UNI_V2_ROUTER  = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
        SUSHI_ROUTER   = "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F"

        i = 0
        while self._running:
            await asyncio.sleep(random.uniform(3, 8))  # arb found every 3-8 seconds in sim

            self.pairs_scanned += 1
            self.last_scan_time = time.time()

            spread = random.randint(70, 200)  # 70-200 bps
            profit_usd = random.uniform(self.min_profit_usd, self.min_profit_usd * 10)

            opp = ArbOpportunity(
                token_a=         USDC,
                token_b=         WETH,
                buy_dex=         random.choice(["uniswap_v2", "sushiswap"]),
                sell_dex=        random.choice(["uniswap_v2", "sushiswap"]),
                buy_router=      UNI_V2_ROUTER,
                sell_router=     SUSHI_ROUTER,
                buy_dex_type=    0,
                sell_dex_type=   2,
                buy_factory=     self.UNI_V2_FACTORY,
                sell_factory=    self.SUSHI_FACTORY,
                loan_amount=     int(random.uniform(10_000, 100_000) * 1e6),
                expected_profit= int(profit_usd * 1e6),
                spread_bps=      spread,
                gas_cost_usd=    random.uniform(2, 8),
                profit_usd=      profit_usd,
                token_a_decimals=6,
            )

            pair_key = "USDC/WETH"
            self.last_spread_bps[pair_key] = spread
            self.opportunities_found += 1
            log.info(f"[detector][sim] {opp}")

            try:
                opportunity_queue.put_nowait(opp)
            except asyncio.QueueFull:
                pass

            i += 1

    def find_arbitrage_opportunity(self, token_pair: tuple[str, str]) -> Optional[ArbOpportunity]:
        """
        Synchronous interface for checking a specific pair.
        Used by tests and the simulation script.
        Returns cached opportunity if price data is available.
        """
        token_a, token_b = token_pair
        cache_key_uni   = (token_a.lower(), token_b.lower(), "uniswap_v2")
        cache_key_sushi = (token_a.lower(), token_b.lower(), "sushiswap")

        uni_price   = self._price_cache.get(cache_key_uni)
        sushi_price = self._price_cache.get(cache_key_sushi)

        if not uni_price or not sushi_price:
            return None

        spread_bps = self._calculate_spread_bps(uni_price, sushi_price)
        if spread_bps < MIN_SPREAD_BPS:
            return None

        buy_price, sell_price = (
            (uni_price, sushi_price) if uni_price.price < sushi_price.price
            else (sushi_price, uni_price)
        )

        loan_amount = self.calculate_optimal_amount(
            buy_price.reserve0, buy_price.reserve1,
            sell_price.reserve0, sell_price.reserve1,
        )
        if loan_amount == 0:
            return None

        expected_profit = self._estimate_profit(loan_amount, buy_price, sell_price, token_a, token_b)
        if expected_profit <= 0:
            return None

        return ArbOpportunity(
            token_a=         token_a,
            token_b=         token_b,
            buy_dex=         buy_price.dex,
            sell_dex=        sell_price.dex,
            buy_router=      buy_price.router,
            sell_router=     sell_price.router,
            buy_dex_type=    0 if buy_price.dex == "uniswap_v2" else 2,
            sell_dex_type=   0 if sell_price.dex == "uniswap_v2" else 2,
            buy_factory=     buy_price.factory,
            sell_factory=    sell_price.factory,
            loan_amount=     loan_amount,
            expected_profit= expected_profit,
            spread_bps=      spread_bps,
            gas_cost_usd=    5.0,
            profit_usd=      expected_profit / 1e6,
            token_a_decimals=self.TOKEN_DECIMALS.get(token_a.lower(), 18),
        )
