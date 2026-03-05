"""
executor.py
===========
Flash loan transaction builder, signer, and submitter.

Responsibilities:
  - Consume ArbOpportunity objects from the queue
  - Build the initiateFlashLoan() transaction calldata
  - Estimate gas with 20% buffer
  - Submit with EIP-1559 priority fee
  - Monitor transaction status (confirmed / reverted)
  - Track P&L per execution
  - Circuit breaker: stop after N consecutive losses
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from eth_account import Account
from eth_abi import encode as abi_encode
from web3 import AsyncWeb3, HTTPProvider
from web3.exceptions import TransactionNotFound

from opportunity_detector import ArbOpportunity

import os
from bot.flashbots_executor import FlashbotsExecutor


log = logging.getLogger("mev.executor")

# ─────────────────────────────────────────────────────────────────────────────
# FlashLoanArb contract ABI (only the functions we call)
# ─────────────────────────────────────────────────────────────────────────────

FLASHLOAN_ARB_ABI = [
    {
        "name": "initiateFlashLoan",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "asset",  "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "params", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "simulateArb",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "asset",  "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "params", "type": "bytes"},
        ],
        "outputs": [{"name": "netProfit", "type": "int256"}],
    },
    {
        "name": "calculateArb",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "tokenA",  "type": "address"},
            {"name": "tokenB",  "type": "address"},
            {"name": "amount",  "type": "uint256"},
            {"name": "dex1",    "type": "tuple",
             "components": [
                 {"name": "dexType", "type": "uint8"},
                 {"name": "router",  "type": "address"},
                 {"name": "factory", "type": "address"},
                 {"name": "quoter",  "type": "address"},
                 {"name": "v3Fee",   "type": "uint24"},
             ]},
            {"name": "dex2",    "type": "tuple",
             "components": [
                 {"name": "dexType", "type": "uint8"},
                 {"name": "router",  "type": "address"},
                 {"name": "factory", "type": "address"},
                 {"name": "quoter",  "type": "address"},
                 {"name": "v3Fee",   "type": "uint24"},
             ]},
        ],
        "outputs": [
            {"name": "profit",  "type": "int256"},
            {"name": "buyOut",  "type": "uint256"},
            {"name": "sellOut", "type": "uint256"},
        ],
    },
    {
        "name": "paused",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "totalExecutions",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionResult:
    """Result of an arb execution attempt."""
    opportunity:    ArbOpportunity
    tx_hash:        Optional[str]
    success:        bool
    gas_used:       int
    gas_price_gwei: float
    actual_profit:  int    # in token_a units (0 if failed)
    profit_usd:     float
    error:          Optional[str]
    submitted_at:   float = field(default_factory=time.time)
    confirmed_at:   Optional[float] = None

    @property
    def latency_ms(self) -> Optional[float]:
        if self.confirmed_at:
            return (self.confirmed_at - self.submitted_at) * 1000
        return None

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        return (
            f"ExecutionResult({status}, profit=${self.profit_usd:.2f}, "
            f"gas={self.gas_used}, tx={self.tx_hash[:10] if self.tx_hash else 'None'}...)"
        )


@dataclass
class GasConfig:
    max_fee_per_gas:      int  # Wei
    max_priority_fee:     int  # Wei
    gas_limit:            int
    base_fee:             int  # Wei

    @property
    def max_fee_gwei(self) -> float:
        return self.max_fee_per_gas / 1e9

    @property
    def priority_gwei(self) -> float:
        return self.max_priority_fee / 1e9


# ─────────────────────────────────────────────────────────────────────────────
# ArbParams encoder
# ─────────────────────────────────────────────────────────────────────────────

def encode_arb_params(opp: ArbOpportunity, min_profit: int) -> bytes:
    """
    ABI-encode an ArbParams struct for the FlashLoanArb contract.

    Solidity struct:
      struct ArbParams {
          address tokenA;
          address tokenB;
          uint256 amountIn;
          DexConfig buyDex;
          DexConfig sellDex;
          uint256 minProfit;
      }

      struct DexConfig {
          uint8   dexType;
          address router;
          address factory;
          address quoter;
          uint24  v3Fee;
      }
    """
    # DexConfig tuple type
    dex_config_type = "(uint8,address,address,address,uint24)"

    # Full ArbParams type
    arb_params_type = f"(address,address,uint256,{dex_config_type},{dex_config_type},uint256)"

    buy_dex_tuple  = (
        opp.buy_dex_type,
        opp.buy_router,
        opp.buy_factory,
        "0x0000000000000000000000000000000000000000",  # quoter — V2 doesn't use it
        0,  # v3Fee — 0 for V2
    )
    sell_dex_tuple = (
        opp.sell_dex_type,
        opp.sell_router,
        opp.sell_factory,
        "0x0000000000000000000000000000000000000000",
        0,
    )

    encoded = abi_encode(
        [arb_params_type],
        [(
            opp.token_a,
            opp.token_b,
            opp.loan_amount,
            buy_dex_tuple,
            sell_dex_tuple,
            min_profit,
        )]
    )
    return encoded


# ─────────────────────────────────────────────────────────────────────────────
# Executor
# ─────────────────────────────────────────────────────────────────────────────

class Executor:
    """
    Builds, signs, and submits flash loan arb transactions.
    Tracks execution history and enforces circuit breaker.
    """

    # Gas estimation buffer: 20%
    GAS_BUFFER_PCT = 1.20

    # Typical flash loan arb gas usage (used as fallback)
    DEFAULT_GAS_LIMIT = 500_000

    # How long to wait for tx confirmation before timing out
    TX_TIMEOUT_SECONDS = 60

    def __init__(
        self,
        rpc_url: str,
        private_key: str,
        contract_address: str,
        gas_optimizer,
        max_consecutive_losses: int = 3,
    ) -> None:
        self.rpc_url           = rpc_url
        self.private_key       = private_key
        self.contract_address  = contract_address
        self.gas_optimizer     = gas_optimizer
        self.max_consec_losses = max_consecutive_losses

        # Wallet
        self._account = Account.from_key(private_key) if private_key else None
        self.wallet_address = self._account.address if self._account else "0x0000000000000000000000000000000000000000"

        # Stats (read by dashboard)
        self.executions:         list[ExecutionResult] = []
        self.total_attempts:     int   = 0
        self.total_successes:    int   = 0
        self.total_failures:     int   = 0
        self.total_profit_usd:   float = 0.0
        self.consecutive_losses: int   = 0
        self.circuit_broken:     bool  = False

        self._running = False
        self._w3: Optional[AsyncWeb3] = None

    async def start(self, opportunity_queue: asyncio.Queue) -> None:
        """Main loop: consume opportunities and execute arbs."""
        self._running = True

        if not self.rpc_url or not self.private_key:
            log.warning("[executor] RPC or private key not set — running in DRY RUN mode")
            await self._dry_run_loop(opportunity_queue)
            return

        self._w3 = AsyncWeb3(HTTPProvider(self.rpc_url))

        # Verify connection
        try:
            chain_id = await self._w3.eth.chain_id
            balance  = await self._w3.eth.get_balance(self.wallet_address)
            log.info(f"[executor] Connected. Chain: {chain_id}, Wallet: {self.wallet_address}")
            log.info(f"[executor] ETH balance: {balance / 1e18:.4f} ETH")
        except Exception as e:
            log.error(f"[executor] Connection failed: {e}")

        while self._running:
            try:
                opp = await asyncio.wait_for(opportunity_queue.get(), timeout=5.0)
                await self._handle_opportunity(opp)
                opportunity_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"[executor] Unexpected error: {e}", exc_info=True)

        log.info("[executor] Stopped.")

    async def _handle_opportunity(self, opp: ArbOpportunity) -> None:
        """Validate, simulate, then execute a single arb opportunity."""
        if self.circuit_broken:
            log.warning(f"[executor] Circuit breaker OPEN — skipping: {opp}")
            return

        log.info(f"[executor] Processing opportunity: {opp}")
        self.total_attempts += 1

        try:
            # Step 1: On-chain simulation (call simulateArb view function)
            net_profit = await self._simulate_on_chain(opp)
            if net_profit is not None and net_profit <= 0:
                log.info(f"[executor] On-chain sim: not profitable (profit={net_profit}). Skipping.")
                return

            # Step 2: Get gas config
            gas_config = await self.gas_optimizer.get_optimal_gas("high")
            gas_gwei = gas_config.max_fee_per_gas / 1e9
            if gas_gwei > 50:  # Max gas guard
                log.warning(f"[executor] Gas too high: {gas_gwei:.1f} Gwei. Skipping.")
                return

            # Step 3: Build transaction
            tx = await self._build_transaction(opp, gas_config)
            if not tx:
                return

            # Step 4: Sign and send
            result = await self._sign_and_send(tx, opp, gas_config)
            self._record_result(result)

        except Exception as e:
            log.error(f"[executor] Error handling opportunity: {e}", exc_info=True)
            self._record_result(ExecutionResult(
                opportunity=opp, tx_hash=None, success=False,
                gas_used=0, gas_price_gwei=0, actual_profit=0,
                profit_usd=0.0, error=str(e)
            ))

    async def _simulate_on_chain(self, opp: ArbOpportunity) -> Optional[int]:
        """Call simulateArb() on the contract to verify profitability right before execution."""
        if not self._w3 or not self.contract_address:
            return None

        try:
            contract = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self.contract_address),
                abi=FLASHLOAN_ARB_ABI
            )
            # minProfit = 50% of expected profit (slippage tolerance)
            min_profit = opp.expected_profit // 2
            params = encode_arb_params(opp, min_profit)

            net_profit = await contract.functions.simulateArb(
                self._w3.to_checksum_address(opp.token_a),
                opp.loan_amount,
                params
            ).call()

            log.debug(f"[executor] On-chain sim profit: {net_profit}")
            return int(net_profit)
        except Exception as e:
            log.debug(f"[executor] simulateArb call failed: {e}")
            return None  # Proceed anyway if simulation call fails

    async def _build_transaction(self, opp: ArbOpportunity, gas_config: GasConfig) -> Optional[dict]:
        """Build the initiateFlashLoan transaction dict."""
        try:
            contract = self._w3.eth.contract(
                address=self._w3.to_checksum_address(self.contract_address),
                abi=FLASHLOAN_ARB_ABI
            )

            min_profit = opp.expected_profit // 2  # 50% of expected = slippage floor
            params     = encode_arb_params(opp, min_profit)

            # Build transaction object for gas estimation
            tx_data = contract.functions.initiateFlashLoan(
                self._w3.to_checksum_address(opp.token_a),
                opp.loan_amount,
                params
            )

            # Estimate gas
            nonce = await self._w3.eth.get_transaction_count(self.wallet_address, "pending")
            chain_id = await self._w3.eth.chain_id

            try:
                estimated_gas = await tx_data.estimate_gas({"from": self.wallet_address})
                gas_limit = int(estimated_gas * self.GAS_BUFFER_PCT)
            except Exception as e:
                log.warning(f"[executor] Gas estimation failed: {e}. Using default.")
                gas_limit = self.DEFAULT_GAS_LIMIT

            tx = tx_data.build_transaction({
                "from":                 self.wallet_address,
                "nonce":                nonce,
                "gas":                  gas_limit,
                "maxFeePerGas":         gas_config.max_fee_per_gas,
                "maxPriorityFeePerGas": gas_config.max_priority_fee,
                "chainId":              chain_id,
                "type":                 2,  # EIP-1559
            })
            return tx

        except Exception as e:
            log.error(f"[executor] Failed to build transaction: {e}", exc_info=True)
            return None

    async def _sign_and_send(
        self,
        tx: dict,
        opp: ArbOpportunity,
        gas_config: GasConfig
    ) -> ExecutionResult:
        """Sign, broadcast, and monitor the transaction."""
        submitted_at = time.time()
        tx_hash_str  = None

        try:
            # Sign
            signed = self._account.sign_transaction(tx)
            tx_hash = await self._w3.eth.send_raw_transaction(signed.rawTransaction)
            tx_hash_str = tx_hash.hex()

            log.info(f"[executor] Tx submitted: {tx_hash_str}")
            log.info(f"[executor] Gas: {gas_config.max_fee_gwei:.1f} Gwei, limit: {tx['gas']}")

            # Wait for confirmation with timeout
            receipt = await asyncio.wait_for(
                self._wait_for_receipt(tx_hash),
                timeout=self.TX_TIMEOUT_SECONDS
            )

            confirmed_at = time.time()
            success      = receipt["status"] == 1
            gas_used     = int(receipt["gasUsed"])
            gas_price    = gas_config.max_fee_per_gas / 1e9

            if success:
                self.total_successes    += 1
                self.consecutive_losses  = 0
                log.info(f"[executor] SUCCESS tx={tx_hash_str[:16]}... gas_used={gas_used}")
                return ExecutionResult(
                    opportunity=    opp,
                    tx_hash=        tx_hash_str,
                    success=        True,
                    gas_used=       gas_used,
                    gas_price_gwei= gas_price,
                    actual_profit=  opp.expected_profit,  # actual profit parsed from events in prod
                    profit_usd=     opp.profit_usd,
                    error=          None,
                    submitted_at=   submitted_at,
                    confirmed_at=   confirmed_at,
                )
            else:
                self.total_failures += 1
                self.consecutive_losses += 1
                log.warning(f"[executor] REVERTED tx={tx_hash_str[:16]}...")
                self._check_circuit_breaker()
                return ExecutionResult(
                    opportunity=    opp,
                    tx_hash=        tx_hash_str,
                    success=        False,
                    gas_used=       gas_used,
                    gas_price_gwei= gas_price,
                    actual_profit=  0,
                    profit_usd=     0.0,
                    error=          "Transaction reverted",
                    submitted_at=   submitted_at,
                    confirmed_at=   confirmed_at,
                )

        except asyncio.TimeoutError:
            self.total_failures += 1
            self.consecutive_losses += 1
            self._check_circuit_breaker()
            return ExecutionResult(
                opportunity=opp, tx_hash=tx_hash_str, success=False,
                gas_used=0, gas_price_gwei=0, actual_profit=0,
                profit_usd=0.0, error="Confirmation timeout"
            )
        except Exception as e:
            self.total_failures += 1
            self.consecutive_losses += 1
            self._check_circuit_breaker()
            log.error(f"[executor] Send error: {e}")
            return ExecutionResult(
                opportunity=opp, tx_hash=tx_hash_str, success=False,
                gas_used=0, gas_price_gwei=0, actual_profit=0,
                profit_usd=0.0, error=str(e)
            )

    async def _wait_for_receipt(self, tx_hash) -> dict:
        """Poll for transaction receipt every 2 seconds."""
        while True:
            try:
                receipt = await self._w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    return dict(receipt)
            except TransactionNotFound:
                pass
            await asyncio.sleep(2.0)

    def _check_circuit_breaker(self) -> None:
        """Trip the circuit breaker if too many consecutive losses."""
        if self.consecutive_losses >= self.max_consec_losses:
            self.circuit_broken = True
            log.critical(
                f"[executor] CIRCUIT BREAKER TRIPPED after "
                f"{self.consecutive_losses} consecutive losses. "
                f"Bot halted. Manual reset required."
            )

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker after investigation."""
        self.circuit_broken     = False
        self.consecutive_losses = 0
        log.info("[executor] Circuit breaker reset.")

    def _record_result(self, result: ExecutionResult) -> None:
        """Store result and update running totals."""
        self.executions.append(result)
        if len(self.executions) > 1000:
            self.executions = self.executions[-500:]  # Keep last 500

        if result.success:
            self.total_profit_usd += result.profit_usd

    def get_recent_executions(self, n: int = 10) -> list[ExecutionResult]:
        return self.executions[-n:]

    def get_success_rate(self) -> float:
        total = self.total_successes + self.total_failures
        return (self.total_successes / total * 100) if total > 0 else 0.0

    async def _dry_run_loop(self, opportunity_queue: asyncio.Queue) -> None:
        """Simulate execution without sending real transactions."""
        import random
        log.info("[executor] DRY RUN mode — no real transactions will be sent")

        while self._running:
            try:
                opp = await asyncio.wait_for(opportunity_queue.get(), timeout=5.0)
                log.info(f"[executor][DRY RUN] Would execute: {opp}")

                # Simulate 90% success rate in dry run
                success = random.random() < 0.9
                profit  = opp.profit_usd if success else 0.0

                result = ExecutionResult(
                    opportunity=    opp,
                    tx_hash=        f"0xdryrun{'0'*60}{self.total_attempts:04d}"[:66],
                    success=        success,
                    gas_used=       random.randint(350_000, 500_000),
                    gas_price_gwei= random.uniform(20, 40),
                    actual_profit=  int(opp.expected_profit) if success else 0,
                    profit_usd=     profit,
                    error=          None if success else "Simulated revert",
                    confirmed_at=   time.time() + random.uniform(12, 30),
                )

                self.total_attempts += 1
                if success:
                    self.total_successes += 1
                    self.consecutive_losses = 0
                    self.total_profit_usd  += profit
                else:
                    self.total_failures += 1
                    self.consecutive_losses += 1
                    self._check_circuit_breaker()

                self._record_result(result)
                opportunity_queue.task_done()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

# Config
class Config:
    use_flashbots: bool = os.environ.get("USE_FLASHBOTS", "false").lower() == "true"
    flashbots_auth_key: str = os.environ.get("FLASHBOTS_AUTH_KEY", "")

    # Flashbots dispatch added
        if self.config.use_flashbots:
            fb = FlashbotsExecutor(self.w3, self.contract, self.config)
            return await fb.execute_arb(opportunity)
