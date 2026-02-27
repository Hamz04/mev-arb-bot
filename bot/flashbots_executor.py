"""
flashbots_executor.py
Flashbots bundle submission for MEV arbitrage.
Submits signed transactions as Flashbots bundles to avoid public mempool front-running.
"""

import asyncio
import logging
import time
import os
from dataclasses import dataclass, field
from typing import Optional
import aiohttp
from eth_account import Account
from eth_account.messages import encode_defunct
import json

logger = logging.getLogger(__name__)

FLASHBOTS_RELAY_MAINNET = "https://relay.flashbots.net"
FLASHBOTS_RELAY_SEPOLIA = "https://relay-sepolia.flashbots.net"
MEV_SHARE_MAINNET = "https://mev-share.flashbots.net"


@dataclass
class BundleResult:
    submitted: bool = False
    included: bool = False
    block_number: int = 0
    bundle_hash: str = ""
    profit_wei: int = 0
    error: Optional[str] = None


@dataclass
class FlashbotsStats:
    bundles_submitted: int = 0
    bundles_included: int = 0
    bundles_failed: int = 0
    total_profit_wei: int = 0

    @property
    def inclusion_rate(self) -> float:
        if self.bundles_submitted == 0:
            return 0.0
        return self.bundles_included / self.bundles_submitted

    @property
    def total_profit_eth(self) -> float:
        return self.total_profit_wei / 1e18


class FlashbotsExecutor:
    """
    Submits MEV arbitrage transactions as Flashbots bundles.

    Bundles are signed with a separate auth key (FLASHBOTS_AUTH_KEY)
    that builds Flashbots reputation independently of the main wallet.
    Each opportunity is submitted for blocks N, N+1, N+2 (standard pattern).
    """

    def __init__(self, web3, contract, config):
        self.w3 = web3
        self.contract = contract
        self.config = config
        self.stats = FlashbotsStats()

        # Auth key for Flashbots reputation (separate from signing key)
        auth_key = os.environ.get("FLASHBOTS_AUTH_KEY", "")
        if not auth_key:
            raise ValueError("FLASHBOTS_AUTH_KEY env var required for Flashbots mode")
        self.auth_account = Account.from_key(auth_key)

        # Relay endpoint
        use_sepolia = getattr(config, "use_sepolia", False)
        self.relay_url = FLASHBOTS_RELAY_SEPOLIA if use_sepolia else FLASHBOTS_RELAY_MAINNET
        logger.info(f"FlashbotsExecutor initialized. Relay: {self.relay_url}")

    def _sign_flashbots_payload(self, payload_body: str) -> str:
        """Sign the payload body with the auth key for Flashbots X-Flashbots-Signature header."""
        body_hash = self.w3.keccak(text=payload_body).hex()
        msg = encode_defunct(text=body_hash)
        signed = Account.sign_message(msg, private_key=self.auth_account.key)
        return f"{self.auth_account.address}:{signed.signature.hex()}"

    async def _rpc_call(self, method: str, params: list) -> dict:
        """Make a signed JSON-RPC call to the Flashbots relay."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload)
        signature = self._sign_flashbots_payload(body)
        headers = {
            "Content-Type": "application/json",
            "X-Flashbots-Signature": signature,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(self.relay_url, data=body, headers=headers) as resp:
                result = await resp.json()
                if "error" in result:
                    raise RuntimeError(f"Flashbots RPC error: {result['error']}")
                return result.get("result", {})

    async def simulate_bundle(self, signed_txs: list[str], block_number: int) -> dict:
        """
        Simulate a bundle via eth_callBundle before submission.
        Returns simulation result with coinbaseDiff (miner payment) and per-tx results.
        """
        block_hex = hex(block_number)
        params = [{
            "txs": signed_txs,
            "blockNumber": block_hex,
            "stateBlockNumber": "latest",
        }]
        result = await self._rpc_call("eth_callBundle", params)
        logger.debug(f"Bundle simulation result: coinbaseDiff={result.get('coinbaseDiff')}")
        return result

    async def submit_bundle(self, signed_txs: list[str], target_block: int) -> str:
        """
        Submit a bundle to the Flashbots relay for a specific target block.
        Returns the bundle hash.
        """
        params = [{
            "txs": signed_txs,
            "blockNumber": hex(target_block),
        }]
        result = await self._rpc_call("eth_sendBundle", params)
        bundle_hash = result.get("bundleHash", "")
        logger.info(f"Bundle submitted for block {target_block}: {bundle_hash}")
        return bundle_hash

    async def _wait_for_inclusion(self, bundle_hash: str, target_block: int, timeout: int = 30) -> bool:
        """Poll for bundle inclusion by checking if target block has been mined."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            current_block = self.w3.eth.block_number
            if current_block >= target_block + 2:
                try:
                    stats = await self._rpc_call("flashbots_getBundleStats", [
                        {"bundleHash": bundle_hash, "blockNumber": hex(target_block)}
                    ])
                    is_included = stats.get("isSimulated", False) and stats.get("isSentToMiners", False)
                    return is_included
                except Exception:
                    return False
            await asyncio.sleep(2)
        return False

    def _build_arb_transaction(self, opportunity) -> dict:
        """Build the raw transaction dict for initiateFlashLoan."""
        account = Account.from_key(self.config.private_key)
        nonce = self.w3.eth.get_transaction_count(account.address)
        latest = self.w3.eth.get_block("latest")
        base_fee = latest["baseFeePerGas"]
        priority_fee = self.w3.to_wei(3, "gwei")  # 3 gwei tip for miners
        max_fee = base_fee * 2 + priority_fee

        tx = self.contract.functions.initiateFlashLoan(
            opportunity.token_address,
            opportunity.optimal_amount,
            opportunity.buy_dex.__dict__,
            opportunity.sell_dex.__dict__,
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "gas": 500_000,
            "chainId": self.w3.eth.chain_id,
        })
        return tx

    async def execute_arb(self, opportunity) -> BundleResult:
        """
        Full Flashbots execution flow:
        1. Build transaction
        2. Sign it
        3. Simulate bundle
        4. Submit for blocks N, N+1, N+2
        5. Wait for inclusion
        """
        result = BundleResult()
        try:
            account = Account.from_key(self.config.private_key)
            tx = self._build_arb_transaction(opportunity)
            signed_tx = account.sign_transaction(tx)
            raw_tx = signed_tx.rawTransaction.hex()
            if not raw_tx.startswith("0x"):
                raw_tx = "0x" + raw_tx

            current_block = self.w3.eth.block_number

            # Simulate first
            sim = await self.simulate_bundle([raw_tx], current_block + 1)
            coinbase_diff = int(sim.get("coinbaseDiff", "0x0"), 16)
            if coinbase_diff == 0:
                result.error = "Simulation shows zero profit — skipping submission"
                logger.warning(result.error)
                return result

            # Submit for next 3 blocks
            bundle_hashes = []
            for offset in range(1, 4):
                target = current_block + offset
                bh = await self.submit_bundle([raw_tx], target)
                bundle_hashes.append((bh, target))
                self.stats.bundles_submitted += 1

            result.submitted = True

            # Wait for first inclusion
            for bh, target_block in bundle_hashes:
                included = await self._wait_for_inclusion(bh, target_block)
                if included:
                    result.included = True
                    result.block_number = target_block
                    result.bundle_hash = bh
                    result.profit_wei = coinbase_diff
                    self.stats.bundles_included += 1
                    self.stats.total_profit_wei += coinbase_diff
                    logger.info(
                        f"Bundle INCLUDED in block {target_block}! "
                        f"Profit: {coinbase_diff / 1e18:.6f} ETH"
                    )
                    return result

            logger.info("Bundle not included in any of 3 target blocks.")
            return result

        except Exception as e:
            result.error = str(e)
            self.stats.bundles_failed += 1
            logger.error(f"FlashbotsExecutor error: {e}", exc_info=True)
            return result

    def get_stats(self) -> dict:
        return {
            "bundles_submitted": self.stats.bundles_submitted,
            "bundles_included": self.stats.bundles_included,
            "bundles_failed": self.stats.bundles_failed,
            "inclusion_rate": f"{self.stats.inclusion_rate:.1%}",
            "total_profit_eth": f"{self.stats.total_profit_eth:.6f}",
        }
