"""
mempool_scanner.py
==================
WebSocket mempool scanner for Ethereum pending transactions.

Subscribes to eth_subscribe("newPendingTransactions") via WebSocket,
filters for DEX swap transactions (Uniswap V2/V3, Sushiswap),
decodes the calldata, extracts token pairs and amounts,
and pushes SwapEvent objects onto an asyncio.Queue for the detector.

Handles:
  - WebSocket reconnection with exponential backoff
  - ABI decoding of V2 and V3 swap calldata
  - Rate limiting to avoid overwhelming the queue
  - Graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from web3 import AsyncWeb3, WebSocketProvider
from web3.types import TxData
from eth_abi import decode as abi_decode

log = logging.getLogger("mev.scanner")

# ─────────────────────────────────────────────────────────────────────────────
# Known DEX function selectors (first 4 bytes of keccak256(signature))
# ─────────────────────────────────────────────────────────────────────────────

# Uniswap V2 / Sushiswap
SEL_V2_SWAP_EXACT_ETH_FOR_TOKENS        = bytes.fromhex("7ff36ab5")
SEL_V2_SWAP_EXACT_TOKENS_FOR_TOKENS     = bytes.fromhex("38ed1739")
SEL_V2_SWAP_TOKENS_FOR_EXACT_TOKENS     = bytes.fromhex("8803dbee")
SEL_V2_SWAP_EXACT_TOKENS_FOR_ETH        = bytes.fromhex("18cbafe5")
SEL_V2_SWAP_ETH_FOR_EXACT_TOKENS        = bytes.fromhex("fb3bdb41")
SEL_V2_SWAP_EXACT_ETH_FOR_TOKENS_FEE    = bytes.fromhex("b6f9de95")

# Uniswap V3
SEL_V3_EXACT_INPUT_SINGLE               = bytes.fromhex("414bf389")
SEL_V3_EXACT_INPUT                      = bytes.fromhex("c04b8d59")
SEL_V3_EXACT_OUTPUT_SINGLE              = bytes.fromhex("db3e2198")
SEL_V3_EXACT_OUTPUT                     = bytes.fromhex("f28c0498")
SEL_V3_MULTICALL                        = bytes.fromhex("ac9650d8")

# All tracked selectors
V2_SELECTORS = {
    SEL_V2_SWAP_EXACT_ETH_FOR_TOKENS,
    SEL_V2_SWAP_EXACT_TOKENS_FOR_TOKENS,
    SEL_V2_SWAP_TOKENS_FOR_EXACT_TOKENS,
    SEL_V2_SWAP_EXACT_TOKENS_FOR_ETH,
    SEL_V2_SWAP_ETH_FOR_EXACT_TOKENS,
    SEL_V2_SWAP_EXACT_ETH_FOR_TOKENS_FEE,
}

V3_SELECTORS = {
    SEL_V3_EXACT_INPUT_SINGLE,
    SEL_V3_EXACT_INPUT,
    SEL_V3_EXACT_OUTPUT_SINGLE,
    SEL_V3_EXACT_OUTPUT,
}

ALL_DEX_SELECTORS = V2_SELECTORS | V3_SELECTORS

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SwapEvent:
    """A decoded DEX swap transaction from the mempool."""
    tx_hash:       str
    from_address:  str
    to_address:    str           # DEX router address
    token_in:      Optional[str] # None if ETH
    token_out:     Optional[str] # None if ETH
    amount_in:     Optional[int]
    amount_out_min:Optional[int]
    dex:           str           # "uniswap_v2", "uniswap_v3", "sushiswap", "unknown"
    swap_version:  int           # 2 or 3
    gas_price:     int           # in Wei
    gas_limit:     int
    timestamp:     float = field(default_factory=time.time)
    raw_input:     bytes = field(default=b"", repr=False)

    @property
    def gas_price_gwei(self) -> float:
        return self.gas_price / 1e9

    def __repr__(self) -> str:
        tin  = self.token_in[:10]  if self.token_in  else "ETH"
        tout = self.token_out[:10] if self.token_out else "ETH"
        return (
            f"SwapEvent(dex={self.dex}, {tin}->{tout}, "
            f"amtIn={self.amount_in}, gas={self.gas_price_gwei:.1f}gwei, "
            f"tx={self.tx_hash[:10]}...)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Calldata decoders
# ─────────────────────────────────────────────────────────────────────────────

def decode_v2_swap(selector: bytes, data: bytes) -> tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    """
    Decode Uniswap V2-style swap calldata.
    Returns: (token_in, token_out, amount_in, amount_out_min)
    """
    try:
        if selector == SEL_V2_SWAP_EXACT_TOKENS_FOR_TOKENS:
            # swapExactTokensForTokens(uint256 amountIn, uint256 amountOutMin, address[] path, address to, uint256 deadline)
            decoded = abi_decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                data
            )
            path = decoded[2]
            return str(path[0]), str(path[-1]), int(decoded[0]), int(decoded[1])

        elif selector == SEL_V2_SWAP_TOKENS_FOR_EXACT_TOKENS:
            # swapTokensForExactTokens(uint256 amountOut, uint256 amountInMax, address[] path, address to, uint256 deadline)
            decoded = abi_decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                data
            )
            path = decoded[2]
            return str(path[0]), str(path[-1]), int(decoded[1]), int(decoded[0])

        elif selector in (SEL_V2_SWAP_EXACT_ETH_FOR_TOKENS, SEL_V2_SWAP_EXACT_ETH_FOR_TOKENS_FEE):
            # swapExactETHForTokens(uint256 amountOutMin, address[] path, address to, uint256 deadline)
            decoded = abi_decode(
                ["uint256", "address[]", "address", "uint256"],
                data
            )
            path = decoded[1]
            return None, str(path[-1]), None, int(decoded[0])  # ETH in

        elif selector == SEL_V2_SWAP_EXACT_TOKENS_FOR_ETH:
            # swapExactTokensForETH(uint256 amountIn, uint256 amountOutMin, address[] path, address to, uint256 deadline)
            decoded = abi_decode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                data
            )
            path = decoded[2]
            return str(path[0]), None, int(decoded[0]), int(decoded[1])  # ETH out

        elif selector == SEL_V2_SWAP_ETH_FOR_EXACT_TOKENS:
            # swapETHForExactTokens(uint256 amountOut, address[] path, address to, uint256 deadline)
            decoded = abi_decode(
                ["uint256", "address[]", "address", "uint256"],
                data
            )
            path = decoded[1]
            return None, str(path[-1]), None, int(decoded[0])

    except Exception:
        pass
    return None, None, None, None


def decode_v3_exact_input_single(data: bytes) -> tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    """
    Decode Uniswap V3 exactInputSingle calldata.
    Struct: (address tokenIn, address tokenOut, uint24 fee, address recipient,
             uint256 deadline, uint256 amountIn, uint256 amountOutMinimum, uint160 sqrtPriceLimitX96)
    """
    try:
        decoded = abi_decode(
            ["address", "address", "uint24", "address", "uint256", "uint256", "uint256", "uint160"],
            data
        )
        return str(decoded[0]), str(decoded[1]), int(decoded[5]), int(decoded[6])
    except Exception:
        pass
    return None, None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# MempoolScanner
# ─────────────────────────────────────────────────────────────────────────────

class MempoolScanner:
    """
    Subscribes to Ethereum mempool via WebSocket and filters DEX swaps.

    Produces SwapEvent objects on self.swap_queue for consumption
    by the OpportunityDetector.
    """

    # Reconnect settings
    RECONNECT_DELAY_MIN = 1.0    # seconds
    RECONNECT_DELAY_MAX = 60.0   # seconds
    RECONNECT_BACKOFF   = 2.0    # multiplier

    # Stats
    def __init__(
        self,
        ws_url: str,
        rpc_url: str,
        uni_v2_router: str,
        uni_v3_router: str,
        sushi_router:  str,
        queue_maxsize: int = 500,
    ) -> None:
        self.ws_url       = ws_url
        self.rpc_url      = rpc_url
        self.queue_maxsize = queue_maxsize

        # Known DEX router addresses (lowercase for fast lookup)
        self.dex_routers: dict[str, str] = {
            uni_v2_router.lower():  "uniswap_v2",
            uni_v3_router.lower():  "uniswap_v3",
            sushi_router.lower():   "sushiswap",
        }

        # Output queue consumed by OpportunityDetector
        self.swap_queue: asyncio.Queue[SwapEvent] = asyncio.Queue(maxsize=queue_maxsize)

        # Stats (read by dashboard)
        self.txs_seen:     int = 0
        self.txs_filtered: int = 0
        self.swaps_decoded:int = 0
        self.reconnects:   int = 0
        self.is_connected: bool = False
        self.last_tx_time: Optional[float] = None

        self._running = False
        self._w3: Optional[AsyncWeb3] = None

    async def start(self) -> None:
        """Start scanning — reconnects automatically on disconnect."""
        self._running = True
        reconnect_delay = self.RECONNECT_DELAY_MIN

        while self._running:
            try:
                await self._connect_and_scan()
                reconnect_delay = self.RECONNECT_DELAY_MIN  # reset on clean disconnect
            except asyncio.CancelledError:
                log.info("[scanner] Cancelled.")
                break
            except Exception as e:
                self.is_connected = False
                self.reconnects += 1
                log.warning(
                    f"[scanner] WebSocket error: {e}. "
                    f"Reconnecting in {reconnect_delay:.1f}s... (attempt #{self.reconnects})"
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * self.RECONNECT_BACKOFF,
                    self.RECONNECT_DELAY_MAX
                )

        log.info("[scanner] Stopped.")

    async def stop(self) -> None:
        self._running = False

    async def _connect_and_scan(self) -> None:
        """Open WebSocket connection and subscribe to pending transactions."""
        if not self.ws_url:
            log.warning("[scanner] WEBSOCKET_URL not set — running in simulation mode")
            await self._simulation_mode()
            return

        log.info(f"[scanner] Connecting to {self.ws_url[:40]}...")

        async with AsyncWeb3(WebSocketProvider(self.ws_url)) as w3:
            self._w3 = w3
            self.is_connected = True
            log.info("[scanner] Connected. Subscribing to pending transactions...")

            # Subscribe to new pending transactions
            sub_id = await w3.eth.subscribe("newPendingTransactions")
            log.info(f"[scanner] Subscribed. Subscription ID: {sub_id}")

            async for message in w3.socket.process_subscriptions():
                if not self._running:
                    break

                tx_hash = message.get("result") or message.get("params", {}).get("result")
                if not tx_hash:
                    continue

                self.txs_seen += 1
                self.last_tx_time = time.time()

                # Fetch full transaction
                try:
                    tx = await w3.eth.get_transaction(tx_hash)
                    await self._process_transaction(tx)
                except Exception:
                    pass  # Transaction may have already been mined or dropped

    async def _process_transaction(self, tx: TxData) -> None:
        """Filter and decode a transaction, push to queue if it's a DEX swap."""
        if not tx or not tx.get("input") or tx.get("input") == "0x":
            return

        to_addr = tx.get("to", "").lower() if tx.get("to") else ""
        dex_name = self.dex_routers.get(to_addr)
        if not dex_name:
            return  # Not a known DEX router

        self.txs_filtered += 1

        # Parse calldata
        input_data = bytes.fromhex(tx["input"][2:]) if isinstance(tx["input"], str) else bytes(tx["input"])
        if len(input_data) < 4:
            return

        selector = input_data[:4]
        if selector not in ALL_DEX_SELECTORS:
            return

        calldata = input_data[4:]

        # Decode based on selector
        token_in, token_out, amount_in, amount_out_min = None, None, None, None
        swap_version = 2

        if selector in V2_SELECTORS:
            token_in, token_out, amount_in, amount_out_min = decode_v2_swap(selector, calldata)
            swap_version = 2
        elif selector == SEL_V3_EXACT_INPUT_SINGLE:
            token_in, token_out, amount_in, amount_out_min = decode_v3_exact_input_single(calldata)
            swap_version = 3
        elif selector in V3_SELECTORS:
            # Other V3 paths — extract what we can
            swap_version = 3

        gas_price = int(tx.get("gasPrice") or tx.get("maxFeePerGas") or 0)
        gas_limit  = int(tx.get("gas", 0))

        event = SwapEvent(
            tx_hash=       tx["hash"].hex() if hasattr(tx["hash"], "hex") else str(tx["hash"]),
            from_address=  tx.get("from", ""),
            to_address=    tx.get("to", ""),
            token_in=      token_in,
            token_out=     token_out,
            amount_in=     amount_in,
            amount_out_min=amount_out_min,
            dex=           dex_name,
            swap_version=  swap_version,
            gas_price=     gas_price,
            gas_limit=     gas_limit,
            raw_input=     input_data,
        )

        self.swaps_decoded += 1
        log.debug(f"[scanner] Decoded: {event}")

        # Non-blocking put — drop if queue is full
        try:
            self.swap_queue.put_nowait(event)
        except asyncio.QueueFull:
            log.debug("[scanner] swap_queue full — dropping swap event")

    async def _simulation_mode(self) -> None:
        """
        Simulation mode when no WebSocket URL is configured.
        Generates synthetic swap events for testing the detector.
        """
        import random
        log.info("[scanner] Running in SIMULATION mode (no WebSocket URL configured)")

        WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
        DAI  = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
        WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"

        pairs = [
            (USDC, WETH), (WETH, USDC), (USDT, WETH),
            (DAI, WETH),  (WBTC, WETH), (USDC, USDT),
        ]
        dexes = ["uniswap_v2", "uniswap_v3", "sushiswap"]

        i = 0
        while self._running:
            pair = random.choice(pairs)
            event = SwapEvent(
                tx_hash=        f"0xsim{i:064x}",
                from_address=   "0xSimulatedSender",
                to_address=     "0xSimulatedRouter",
                token_in=       pair[0],
                token_out=      pair[1],
                amount_in=      random.randint(1_000_000, 1_000_000_000_000),
                amount_out_min= 1,
                dex=            random.choice(dexes),
                swap_version=   random.choice([2, 3]),
                gas_price=      int(random.uniform(10, 50) * 1e9),
                gas_limit=      250_000,
            )

            self.txs_seen     += 1
            self.txs_filtered += 1
            self.swaps_decoded+= 1
            self.is_connected  = True
            self.last_tx_time  = time.time()

            try:
                self.swap_queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

            i += 1
            await asyncio.sleep(random.uniform(0.1, 0.5))  # ~2-10 swaps/second
