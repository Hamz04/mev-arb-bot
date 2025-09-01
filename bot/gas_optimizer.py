"""
gas_optimizer.py
================
EIP-1559 gas price oracle and optimizer.

Fetches real-time base fee from the latest block, calculates
optimal priority fee (miner tip), and predicts max fee per gas
based on urgency level.

Also maintains a rolling history of gas prices for the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from web3 import AsyncWeb3, HTTPProvider

log = logging.getLogger("mev.gas")


@dataclass
class GasConfig:
    """EIP-1559 gas parameters for a transaction."""
    max_fee_per_gas:   int    # Wei — total max (base fee + priority)
    max_priority_fee:  int    # Wei — miner tip
    gas_limit:         int    # Units
    base_fee:          int    # Wei — current block base fee
    urgency:           str    # "low", "medium", "high"

    @property
    def max_fee_gwei(self) -> float:
        return self.max_fee_per_gas / 1e9

    @property
    def priority_gwei(self) -> float:
        return self.max_priority_fee / 1e9

    @property
    def base_fee_gwei(self) -> float:
        return self.base_fee / 1e9

    def __repr__(self) -> str:
        return (
            f"GasConfig(urgency={self.urgency}, "
            f"base={self.base_fee_gwei:.2f}gwei, "
            f"priority={self.priority_gwei:.2f}gwei, "
            f"max={self.max_fee_gwei:.2f}gwei)"
        )


@dataclass
class GasSnapshot:
    """A historical gas price data point."""
    timestamp:      float
    base_fee_gwei:  float
    priority_gwei:  float
    max_fee_gwei:   float
    block_number:   int


class GasOptimizer:
    """
    EIP-1559 gas price calculator.

    Urgency levels and their multipliers on top of base fee:
      low:    base_fee * 1.0 + 1 gwei tip
      medium: base_fee * 1.1 + 1.5 gwei tip
      high:   base_fee * 1.25 + 2 gwei tip (for competitive arb)
      urgent: base_fee * 1.5  + 3 gwei tip (front-run protection)
    """

    URGENCY_PARAMS = {
        "low":    {"base_multiplier": 1.00, "priority_gwei": 1.0},
        "medium": {"base_multiplier": 1.10, "priority_gwei": 1.5},
        "high":   {"base_multiplier": 1.25, "priority_gwei": 2.0},
        "urgent": {"base_multiplier": 1.50, "priority_gwei": 3.0},
    }

    DEFAULT_GAS_LIMIT = 500_000

    def __init__(self, rpc_url: str, history_size: int = 300) -> None:
        self.rpc_url     = rpc_url
        self._w3: Optional[AsyncWeb3] = None
        self._history: deque[GasSnapshot] = deque(maxlen=history_size)
        self._last_fetch: float = 0.0
        self._cache_ttl:  float = 3.0   # Re-fetch every 3 seconds
        self._cached_config: Optional[GasConfig] = None
        self._cached_base_fee: int = int(20e9)  # 20 Gwei default

        # Stats for dashboard
        self.current_base_fee_gwei:  float = 20.0
        self.current_priority_gwei:  float = 1.5
        self.avg_base_fee_gwei_1h:   float = 20.0
        self.min_base_fee_gwei_1h:   float = 20.0
        self.max_base_fee_gwei_1h:   float = 20.0
        self.blocks_fetched:         int   = 0

    async def _ensure_connected(self) -> None:
        if self._w3 is None and self.rpc_url:
            self._w3 = AsyncWeb3(HTTPProvider(self.rpc_url))

    async def get_base_fee(self) -> int:
        """Fetch the current block's base fee in Wei."""
        await self._ensure_connected()
        if not self._w3:
            return self._cached_base_fee

        try:
            block = await self._w3.eth.get_block("latest")
            base_fee = int(block.get("baseFeePerGas", int(20e9)))
            self._cached_base_fee = base_fee
            self.current_base_fee_gwei = base_fee / 1e9
            self.blocks_fetched += 1
            return base_fee
        except Exception as e:
            log.debug(f"[gas] Failed to fetch base fee: {e}")
            return self._cached_base_fee

    async def get_optimal_gas(
        self,
        urgency: str = "medium",
        gas_limit: int = DEFAULT_GAS_LIMIT,
    ) -> GasConfig:
        """
        Calculate optimal EIP-1559 gas parameters.

        Args:
            urgency:   "low" | "medium" | "high" | "urgent"
            gas_limit: Transaction gas limit (default 500k for flash loan arb)

        Returns:
            GasConfig with max_fee_per_gas, max_priority_fee, gas_limit
        """
        # Use cache if fresh
        now = time.time()
        if self._cached_config and (now - self._last_fetch) < self._cache_ttl:
            # Update gas limit but keep gas prices
            return GasConfig(
                max_fee_per_gas  = self._cached_config.max_fee_per_gas,
                max_priority_fee = self._cached_config.max_priority_fee,
                gas_limit        = gas_limit,
                base_fee         = self._cached_config.base_fee,
                urgency          = urgency,
            )

        base_fee = await self.get_base_fee()
        params   = self.URGENCY_PARAMS.get(urgency, self.URGENCY_PARAMS["medium"])

        priority_fee = int(params["priority_gwei"] * 1e9)
        max_fee      = int(base_fee * params["base_multiplier"]) + priority_fee

        config = GasConfig(
            max_fee_per_gas  = max_fee,
            max_priority_fee = priority_fee,
            gas_limit        = gas_limit,
            base_fee         = base_fee,
            urgency          = urgency,
        )

        self._cached_config = config
        self._last_fetch    = now
        self.current_priority_gwei = priority_fee / 1e9

        # Record history snapshot
        snapshot = GasSnapshot(
            timestamp=     now,
            base_fee_gwei= base_fee / 1e9,
            priority_gwei= priority_fee / 1e9,
            max_fee_gwei=  max_fee / 1e9,
            block_number=  self.blocks_fetched,
        )
        self._history.append(snapshot)
        self._update_stats()

        log.debug(f"[gas] {config}")
        return config

    def _update_stats(self) -> None:
        """Recompute rolling statistics from history."""
        if not self._history:
            return
        # Last hour = last 300 blocks ~= 60 min at 12s/block
        recent = list(self._history)
        base_fees = [s.base_fee_gwei for s in recent]
        self.avg_base_fee_gwei_1h = sum(base_fees) / len(base_fees)
        self.min_base_fee_gwei_1h = min(base_fees)
        self.max_base_fee_gwei_1h = max(base_fees)

    def predict_next_base_fee(self) -> float:
        """
        Predict next block's base fee using EIP-1559 formula.
        Each block adjusts by at most ±12.5% based on gas usage vs target.
        If current block is full (target = 50%), base fee increases 12.5%.
        Conservative estimate: assume 60% full -> ~7.5% increase.
        """
        current = self._cached_base_fee
        # Assume moderately congested: +5% estimate
        return (current * 1.05) / 1e9

    def get_gas_history(self, n: int = 60) -> list[GasSnapshot]:
        """Return last N gas snapshots for charting."""
        history = list(self._history)
        return history[-n:] if len(history) >= n else history

    def is_gas_acceptable(self, max_gas_gwei: float) -> bool:
        """Check if current gas is within the configured maximum."""
        return self.current_base_fee_gwei <= max_gas_gwei

    async def start_polling(self, interval: float = 12.0) -> None:
        """Background task: poll gas every block (~12 seconds)."""
        log.info("[gas] Starting gas price polling...")
        while True:
            try:
                await self.get_optimal_gas("medium")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug(f"[gas] Poll error: {e}")
            await asyncio.sleep(interval)

    async def estimate_arb_cost_usd(
        self,
        eth_price_usd: float = 2500.0,
        urgency: str = "high",
    ) -> float:
        """Estimate the USD cost of executing one flash loan arb transaction."""
        config   = await self.get_optimal_gas(urgency, gas_limit=450_000)
        cost_eth = (config.gas_limit * config.max_fee_per_gas) / 1e18
        return cost_eth * eth_price_usd

    @staticmethod
    def wei_to_gwei(wei: int) -> float:
        return wei / 1e9

    @staticmethod
    def gwei_to_wei(gwei: float) -> int:
        return int(gwei * 1e9)
