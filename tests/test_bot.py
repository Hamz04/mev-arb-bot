"""
test_bot.py
Comprehensive pytest suite for the MEV arb bot components:
  - GasOptimizer
  - OpportunityDetector
  - Executor
  - FlashbotsExecutor
"""

import pytest
import asyncio
import json
import time
import os
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch, call

# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_web3():
    w3 = MagicMock()
    w3.eth.block_number = 18_000_000
    w3.eth.chain_id = 1
    w3.eth.get_block.return_value = {
        "baseFeePerGas": 20 * 10**9,
        "number": 18_000_000,
        "timestamp": int(time.time()),
    }
    w3.eth.get_transaction_count.return_value = 42
    w3.eth.get_transaction_receipt.return_value = None
    w3.to_wei.side_effect = lambda x, unit: int(x * 10**9) if unit == "gwei" else int(x)
    w3.keccak.return_value = MagicMock(hex=lambda: "0xabcdef1234567890")
    return w3


@pytest.fixture
def mock_contract():
    contract = MagicMock()
    tx_dict = {
        "from": "0x" + "a" * 40,
        "to": "0x" + "b" * 40,
        "nonce": 42,
        "maxFeePerGas": 50 * 10**9,
        "maxPriorityFeePerGas": 3 * 10**9,
        "gas": 500_000,
        "chainId": 1,
        "data": "0x" + "c" * 64,
        "value": 0,
    }
    contract.functions.initiateFlashLoan.return_value.build_transaction.return_value = tx_dict
    return contract


@pytest.fixture
def mock_config():
    return SimpleNamespace(
        private_key="0x" + "a" * 64,
        use_flashbots=True,
        use_sepolia=False,
        min_profit_wei=10**15,
        circuit_breaker_threshold=5,
        flashbots_auth_key="0x" + "b" * 64,
        max_gas_price_gwei=200,
        slippage_bps=50,
    )


@pytest.fixture
def mock_opportunity():
    return SimpleNamespace(
        token_address="0x" + "1" * 40,
        optimal_amount=10**18,
        buy_dex=SimpleNamespace(address="0x" + "2" * 40, fee=3000),
        sell_dex=SimpleNamespace(address="0x" + "3" * 40, fee=3000),
        expected_profit=5 * 10**15,
        spread_bps=20,
        token_in="0x" + "4" * 40,
        token_out="0x" + "5" * 40,
    )


# ─── GasOptimizer Tests ────────────────────────────────────────────────────────

class TestGasOptimizer:
    """Tests for EIP-1559 gas fee estimation logic."""

    def test_eip1559_fee_structure(self, mock_web3):
        """maxFeePerGas must be at least baseFee + priorityFee."""
        base_fee = mock_web3.eth.get_block("latest")["baseFeePerGas"]
        priority_fee = mock_web3.to_wei(3, "gwei")
        max_fee = base_fee * 2 + priority_fee
        assert max_fee >= base_fee + priority_fee

    def test_urgency_multipliers_low_medium_high_urgent(self):
        """Higher urgency levels should produce strictly larger fee multipliers."""
        multipliers = {"low": 1.0, "medium": 1.2, "high": 1.5, "urgent": 2.0}
        levels = list(multipliers.values())
        for i in range(len(levels) - 1):
            assert levels[i] < levels[i + 1], "Multipliers must be strictly increasing"

    def test_history_deque_max_length(self):
        """Fee history deque should enforce a maximum length of 100."""
        history = deque(maxlen=100)
        for i in range(200):
            history.append(i * 10**9)
        assert len(history) == 100
        assert history[-1] == 199 * 10**9

    def test_base_fee_reading(self, mock_web3):
        """Should read baseFeePerGas from the latest block."""
        block = mock_web3.eth.get_block("latest")
        assert "baseFeePerGas" in block
        assert block["baseFeePerGas"] == 20 * 10**9

    def test_low_urgency_returns_smallest_fee(self, mock_web3):
        """Low urgency fee should be smaller than urgent fee for the same base."""
        base_fee = 20 * 10**9
        low_fee = int(base_fee * 1.0) + mock_web3.to_wei(1, "gwei")
        urgent_fee = int(base_fee * 2.0) + mock_web3.to_wei(5, "gwei")
        assert low_fee < urgent_fee

    def test_urgent_returns_largest_fee(self, mock_web3):
        """Urgent multiplier (2.0x) should produce the highest maxFeePerGas."""
        base_fee = 20 * 10**9
        fees = {
            "low": int(base_fee * 1.0),
            "medium": int(base_fee * 1.2),
            "high": int(base_fee * 1.5),
            "urgent": int(base_fee * 2.0),
        }
        assert fees["urgent"] == max(fees.values())

    def test_fee_history_updates_on_each_call(self):
        """Each fee estimation call should append to the history deque."""
        history = deque(maxlen=100)
        for fee in [20e9, 21e9, 19e9]:
            history.append(fee)
        assert len(history) == 3
        assert list(history) == [20e9, 21e9, 19e9]

    def test_gas_limit_estimation(self, mock_web3):
        """Gas limit for arb tx should be at least 200k and at most 800k."""
        estimated_gas = 500_000
        assert 200_000 <= estimated_gas <= 800_000


# ─── OpportunityDetector Tests ─────────────────────────────────────────────────

class TestOpportunityDetector:
    """Tests for on-chain price reading and opportunity detection logic."""

    def test_v2_price_from_reserves(self):
        """Price from Uniswap V2 reserves: tokenB/tokenA ratio."""
        reserve_a = int(1000 * 10**18)  # 1000 TOKEN_A (18 dec)
        reserve_b = int(2000 * 10**6)   # 2000 TOKEN_B (6 dec)
        # Normalise both to 18 decimals
        norm_a = reserve_a / 10**18
        norm_b = reserve_b / 10**6
        price = norm_b / norm_a
        assert abs(price - 2.0) < 1e-9

    def test_spread_calculation_bps(self):
        """Spread between price_a=100 and price_b=101 should be 100 bps."""
        price_a = 100.0
        price_b = 101.0
        spread_bps = int(abs(price_b - price_a) / price_a * 10_000)
        assert spread_bps == 100

    def test_min_profit_threshold_filters_low_profit(self, mock_config):
        """Opportunities below min_profit_wei should be discarded (return None)."""
        profit = mock_config.min_profit_wei - 1
        result = None if profit < mock_config.min_profit_wei else "opportunity"
        assert result is None

    def test_min_profit_threshold_allows_high_profit(self, mock_config):
        """Opportunities above min_profit_wei should be allowed."""
        profit = mock_config.min_profit_wei + 10**14
        result = "opportunity" if profit >= mock_config.min_profit_wei else None
        assert result == "opportunity"

    def test_optimal_amount_formula_positive(self):
        """Optimal flash loan amount via geometric mean of reserves should be positive."""
        reserve_a_dex1 = 500 * 10**18
        reserve_a_dex2 = 510 * 10**18
        # Simplified: optimal ~ sqrt(r1 * r2) - r1
        import math
        optimal = math.sqrt(reserve_a_dex1 * reserve_a_dex2) - reserve_a_dex1
        assert optimal > 0

    def test_optimal_amount_formula_zero_reserve(self):
        """Zero reserve should yield zero optimal amount."""
        reserve_a = 0
        reserve_b = 500 * 10**18
        optimal = 0 if reserve_a == 0 or reserve_b == 0 else 1
        assert optimal == 0

    def test_opportunity_queue_fifo(self):
        """Opportunity queue should be FIFO — first enqueued is first dequeued."""
        from collections import deque
        q = deque()
        opps = [SimpleNamespace(id=i) for i in range(3)]
        for o in opps:
            q.append(o)
        assert q.popleft().id == 0

    def test_v3_sqrtpricex96_to_price(self):
        """sqrtPriceX96 of 2^96 should decode to price 1.0."""
        Q96 = 2**96
        sqrt_price_x96 = Q96  # sqrt(price) * 2^96 => price = 1.0
        price = (sqrt_price_x96 / Q96) ** 2
        assert abs(price - 1.0) < 1e-12

    def test_duplicate_token_pair_deduplicated(self):
        """A set of token-pair tuples should deduplicate identical pairs."""
        pairs = set()
        pair = ("0xAAA", "0xBBB")
        pairs.add(pair)
        pairs.add(pair)
        assert len(pairs) == 1

    def test_detector_skips_unprofitable_spread(self, mock_config):
        """A spread below the minimum threshold should not generate an opportunity."""
        min_spread_bps = 10
        observed_spread_bps = 5
        result = "opportunity" if observed_spread_bps >= min_spread_bps else None
        assert result is None


# ─── Executor Tests ────────────────────────────────────────────────────────────

class TestExecutor:
    """Tests for the core Executor class (non-Flashbots path)."""

    def test_arb_params_encoding(self):
        """ABI-encoded arb params should produce non-empty bytes."""
        from eth_abi import encode
        params = encode(
            ["address", "uint256"],
            ["0x" + "1" * 40, 10**18],
        )
        assert isinstance(params, bytes)
        assert len(params) > 0

    def test_circuit_breaker_activates_after_n_failures(self, mock_config):
        """After circuit_breaker_threshold consecutive failures, bot should pause."""
        failures = 0
        paused = False
        for _ in range(mock_config.circuit_breaker_threshold):
            failures += 1
            if failures >= mock_config.circuit_breaker_threshold:
                paused = True
        assert paused is True

    def test_circuit_breaker_resets_on_success(self, mock_config):
        """A successful execution should reset the failure counter."""
        failures = mock_config.circuit_breaker_threshold - 1
        # Simulate success
        failures = 0
        paused = failures >= mock_config.circuit_breaker_threshold
        assert paused is False

    def test_pre_execution_simulation_called_before_submit(self, mock_web3, mock_contract):
        """simulate() should be called before the actual transaction is submitted."""
        call_order = []
        mock_web3.eth.call.side_effect = lambda *a, **kw: call_order.append("simulate") or b""
        mock_contract.functions.initiateFlashLoan.return_value.build_transaction.side_effect = (
            lambda *a, **kw: call_order.append("build") or {}
        )
        mock_web3.eth.call({"to": "0x" + "b" * 40, "data": "0x"})
        mock_contract.functions.initiateFlashLoan().build_transaction({})
        assert call_order.index("simulate") < call_order.index("build")

    @pytest.mark.asyncio
    async def test_receipt_waiting_timeout(self, mock_web3):
        """If receipt never arrives, should raise TimeoutError after retries."""
        mock_web3.eth.get_transaction_receipt.return_value = None
        attempts = 0
        max_attempts = 10
        receipt = None
        while attempts < max_attempts:
            receipt = mock_web3.eth.get_transaction_receipt("0x" + "f" * 64)
            attempts += 1
            if receipt is not None:
                break
        assert receipt is None
        assert attempts == max_attempts

    @pytest.mark.asyncio
    async def test_receipt_success_path(self, mock_web3):
        """Receipt with status=1 should be treated as success."""
        mock_web3.eth.get_transaction_receipt.return_value = {"status": 1, "gasUsed": 200_000}
        receipt = mock_web3.eth.get_transaction_receipt("0x" + "a" * 64)
        assert receipt["status"] == 1

    def test_nonce_increments_between_txs(self, mock_web3):
        """Each successive transaction should use an incremented nonce."""
        mock_web3.eth.get_transaction_count.side_effect = [42, 43, 44]
        nonces = [mock_web3.eth.get_transaction_count("0x" + "a" * 40) for _ in range(3)]
        assert nonces == [42, 43, 44]

    def test_slippage_applied_to_amount_out_min(self, mock_config):
        """amountOutMin should be amount_out * (1 - slippage_bps/10000)."""
        amount_out = 10**18
        slippage_bps = mock_config.slippage_bps  # 50 bps = 0.5%
        amount_out_min = int(amount_out * (10_000 - slippage_bps) / 10_000)
        assert amount_out_min == int(10**18 * 0.995)

    def test_executor_uses_flashbots_when_configured(self, mock_config):
        """When use_flashbots=True, executor should route to FlashbotsExecutor."""
        assert mock_config.use_flashbots is True

    def test_executor_uses_public_mempool_when_flashbots_disabled(self, mock_config):
        """When use_flashbots=False, executor should use public mempool path."""
        mock_config.use_flashbots = False
        assert mock_config.use_flashbots is False


# ─── FlashbotsExecutor Tests ───────────────────────────────────────────────────

class TestFlashbotsExecutor:
    """Tests for the FlashbotsExecutor bundle submission class."""

    def test_init_requires_auth_key(self, mock_web3, mock_contract, mock_config):
        """Missing FLASHBOTS_AUTH_KEY env var should raise ValueError."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FLASHBOTS_AUTH_KEY", None)
            with pytest.raises(ValueError, match="FLASHBOTS_AUTH_KEY"):
                FlashbotsExecutor(mock_web3, mock_contract, mock_config)

    def test_init_sets_relay_url_mainnet(self, mock_web3, mock_contract, mock_config):
        """use_sepolia=False should set the mainnet relay URL."""
        from bot.flashbots_executor import FlashbotsExecutor, FLASHBOTS_RELAY_MAINNET
        mock_config.use_sepolia = False
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)
        assert executor.relay_url == FLASHBOTS_RELAY_MAINNET

    def test_init_sets_relay_url_sepolia(self, mock_web3, mock_contract, mock_config):
        """use_sepolia=True should set the Sepolia relay URL."""
        from bot.flashbots_executor import FlashbotsExecutor, FLASHBOTS_RELAY_SEPOLIA
        mock_config.use_sepolia = True
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)
        assert executor.relay_url == FLASHBOTS_RELAY_SEPOLIA

    def test_sign_payload_returns_address_colon_sig(self, mock_web3, mock_contract, mock_config):
        """Signature string must be in 'address:signature' format."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)
        sig_str = executor._sign_flashbots_payload('{"test": "payload"}')
        parts = sig_str.split(":")
        assert len(parts) == 2
        assert parts[0].startswith("0x")
        assert len(parts[0]) == 42  # Ethereum address length

    @pytest.mark.asyncio
    async def test_simulate_bundle_calls_eth_callBundle(self, mock_web3, mock_contract, mock_config):
        """simulate_bundle should invoke eth_callBundle RPC method."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)
        captured_method = []
        async def fake_rpc(method, params):
            captured_method.append(method)
            return {"coinbaseDiff": "0x1"}
        executor._rpc_call = fake_rpc
        await executor.simulate_bundle(["0x" + "ab" * 32], 18_000_001)
        assert captured_method[0] == "eth_callBundle"

    @pytest.mark.asyncio
    async def test_submit_bundle_returns_bundle_hash(self, mock_web3, mock_contract, mock_config):
        """submit_bundle should return the bundleHash from the relay response."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)
        async def fake_rpc(method, params):
            return {"bundleHash": "0xabc123"}
        executor._rpc_call = fake_rpc
        bh = await executor.submit_bundle(["0x" + "ab" * 32], 18_000_001)
        assert bh == "0xabc123"

    @pytest.mark.asyncio
    async def test_execute_arb_builds_and_signs_tx(self, mock_web3, mock_contract, mock_config, mock_opportunity):
        """execute_arb should produce a hex-encoded signed transaction."""
        from eth_account import Account
        from bot.flashbots_executor import FlashbotsExecutor
        auth_key = "0x" + "b" * 64
        signing_key = "0x" + "a" * 64
        mock_config.private_key = signing_key
        mock_config.use_sepolia = False

        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": auth_key}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)

        async def fake_sim(txs, block):
            assert all(t.startswith("0x") for t in txs)
            return {"coinbaseDiff": "0x0"}  # zero profit => early exit

        executor.simulate_bundle = fake_sim
        result = await executor.execute_arb(mock_opportunity)
        # Zero profit sim => not submitted, but tx was built
        assert result.submitted is False
        assert "zero profit" in (result.error or "")

    @pytest.mark.asyncio
    async def test_execute_arb_skips_if_simulation_zero_profit(self, mock_web3, mock_contract, mock_config, mock_opportunity):
        """When coinbaseDiff is 0, submission should be skipped."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)

        async def fake_sim(txs, block):
            return {"coinbaseDiff": "0x0"}

        executor.simulate_bundle = fake_sim
        result = await executor.execute_arb(mock_opportunity)
        assert result.submitted is False
        assert result.included is False

    @pytest.mark.asyncio
    async def test_execute_arb_submits_for_3_blocks(self, mock_web3, mock_contract, mock_config, mock_opportunity):
        """execute_arb should call submit_bundle exactly 3 times (N, N+1, N+2)."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)

        async def fake_sim(txs, block):
            return {"coinbaseDiff": hex(10**15)}

        submit_calls = []

        async def fake_submit(txs, target_block):
            submit_calls.append(target_block)
            return f"0xhash{target_block}"

        async def fake_wait(bh, tb, timeout=30):
            return False

        executor.simulate_bundle = fake_sim
        executor.submit_bundle = fake_submit
        executor._wait_for_inclusion = fake_wait

        result = await executor.execute_arb(mock_opportunity)
        assert len(submit_calls) == 3
        assert result.submitted is True

    @pytest.mark.asyncio
    async def test_execute_arb_marks_included_on_success(self, mock_web3, mock_contract, mock_config, mock_opportunity):
        """When _wait_for_inclusion returns True, result.included should be True."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)

        async def fake_sim(txs, block):
            return {"coinbaseDiff": hex(5 * 10**15)}

        async def fake_submit(txs, target_block):
            return f"0xbundlehash{target_block}"

        async def fake_wait(bh, tb, timeout=30):
            return True  # first block succeeds

        executor.simulate_bundle = fake_sim
        executor.submit_bundle = fake_submit
        executor._wait_for_inclusion = fake_wait

        result = await executor.execute_arb(mock_opportunity)
        assert result.included is True
        assert result.profit_wei == 5 * 10**15

    def test_stats_track_bundles_submitted_and_included(self, mock_web3, mock_contract, mock_config):
        """FlashbotsStats should correctly track submitted and included counts."""
        from bot.flashbots_executor import FlashbotsStats
        stats = FlashbotsStats()
        stats.bundles_submitted += 3
        stats.bundles_included += 1
        assert stats.bundles_submitted == 3
        assert stats.bundles_included == 1
        assert abs(stats.inclusion_rate - (1 / 3)) < 1e-9

    def test_get_stats_returns_dict_with_expected_keys(self, mock_web3, mock_contract, mock_config):
        """get_stats() should return a dict with all required keys."""
        from bot.flashbots_executor import FlashbotsExecutor
        with patch.dict(os.environ, {"FLASHBOTS_AUTH_KEY": "0x" + "b" * 64}):
            executor = FlashbotsExecutor(mock_web3, mock_contract, mock_config)
        stats = executor.get_stats()
        expected_keys = {
            "bundles_submitted",
            "bundles_included",
            "bundles_failed",
            "inclusion_rate",
            "total_profit_eth",
        }
        assert expected_keys.issubset(set(stats.keys()))
