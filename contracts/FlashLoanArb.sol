// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

import "./interfaces/IFlashLoanReceiver.sol";
import "./interfaces/IAavePool.sol";
import "./ArbExecutor.sol";

/**
 * @title FlashLoanArb
 * @author MEV Bot
 * @notice Production-grade MEV arbitrage contract that:
 *         1. Borrows assets via Aave V3 flash loans (0.05% fee)
 *         2. Executes cross-DEX arbitrage in a single atomic transaction
 *         3. Repays the flash loan + premium
 *         4. Sends profit to the owner
 *
 * @dev Inherits ArbExecutor for multi-DEX swap routing.
 *      Implements IFlashLoanSimpleReceiver for Aave V3 callback.
 *
 * Deployment addresses (Sepolia):
 *   Aave V3 Pool:  0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951
 *
 * Aave V3 flash loan fee: 0.05% (5 basis points)
 * The premium is added on top of the borrowed amount.
 *
 * Flash loan flow:
 *   initiateFlashLoan()  -->  Aave Pool
 *       <-- executeOperation() callback (within same tx)
 *           --> _executeSwap(buyDex)   buy tokenB with tokenA
 *           --> _executeSwap(sellDex)  sell tokenB back to tokenA
 *           --> approve repayment (amount + premium)
 *           --> transfer profit to owner
 */
contract FlashLoanArb is IFlashLoanSimpleReceiver, ArbExecutor, Ownable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ─────────────────────────────────────────────────────────────────────────
    // State variables
    // ─────────────────────────────────────────────────────────────────────────

    /// @notice Aave V3 Pool contract
    IAavePool public immutable POOL;

    /// @notice Aave V3 Addresses Provider
    address   public immutable ADDRESSES_PROVIDER;

    /// @notice Total profit accumulated (per token)
    mapping(address => uint256) public totalProfit;

    /// @notice Total number of successful arb executions
    uint256 public totalExecutions;

    /// @notice Total number of failed arb attempts
    uint256 public totalFailures;

    /// @notice Whether the bot is paused (emergency stop)
    bool public paused;

    // ─────────────────────────────────────────────────────────────────────────
    // Events
    // ─────────────────────────────────────────────────────────────────────────

    /// @notice Emitted after a successful arbitrage execution
    event ArbExecuted(
        address indexed token,
        uint256 borrowed,
        uint256 premium,
        uint256 profit,
        address indexed buyDex,
        address indexed sellDex
    );

    /// @notice Emitted when an arb attempt fails (profit < minProfit)
    event ArbFailed(
        address indexed token,
        uint256 borrowed,
        uint256 profit,
        uint256 minProfit,
        string reason
    );

    /// @notice Emitted when tokens are recovered from the contract
    event TokensWithdrawn(address indexed token, uint256 amount, address indexed to);

    /// @notice Emitted when ETH is withdrawn
    event EthWithdrawn(uint256 amount, address indexed to);

    /// @notice Emitted when the paused state changes
    event PausedStateChanged(bool paused);

    // ─────────────────────────────────────────────────────────────────────────
    // Errors
    // ─────────────────────────────────────────────────────────────────────────

    error NotPool();
    error NotInitiator();
    error ContractPaused();
    error InsufficientProfit(uint256 actual, uint256 minimum);
    error RepaymentFailed();
    error ZeroAmount();
    error ZeroAddress();

    // ─────────────────────────────────────────────────────────────────────────
    // Constructor
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @param pool             Aave V3 Pool address
     *                         Sepolia: 0x6Ae43d3271ff6888e7Fc43Fd7321a503ff738951
     *                         Mainnet: 0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2
     * @param addressesProvider Aave V3 PoolAddressesProvider address
     * @param initialOwner     Owner of the contract (receives profits)
     */
    constructor(
        address pool,
        address addressesProvider,
        address initialOwner
    ) Ownable(initialOwner) {
        if (pool == address(0))             revert ZeroAddress();
        if (addressesProvider == address(0)) revert ZeroAddress();

        POOL               = IAavePool(pool);
        ADDRESSES_PROVIDER = addressesProvider;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Modifiers
    // ─────────────────────────────────────────────────────────────────────────

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Flash Loan Entry Point
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Initiate a flash loan arb.  Call this from your off-chain bot.
     * @dev    Encodes ArbParams into bytes and passes to Aave Pool.
     *         The Pool calls executeOperation() back within the same transaction.
     *
     * @param asset   Token to borrow (e.g. USDC, WETH)
     * @param amount  Amount to borrow in token's native decimals
     * @param params  ABI-encoded ArbParams struct
     */
    function initiateFlashLoan(
        address asset,
        uint256 amount,
        bytes calldata params
    ) external onlyOwner whenNotPaused nonReentrant {
        if (amount == 0) revert ZeroAmount();
        if (asset  == address(0)) revert ZeroAddress();

        // Validate that the ArbParams decode correctly before spending gas on flash loan
        ArbParams memory arbParams = abi.decode(params, (ArbParams));
        require(arbParams.tokenA == asset,       "FlashLoanArb: asset/tokenA mismatch");
        require(arbParams.amountIn <= amount,    "FlashLoanArb: amountIn exceeds borrow");
        require(arbParams.tokenB != address(0),  "FlashLoanArb: tokenB is zero address");

        // Aave V3 simple flash loan — single asset, 0.05% fee
        POOL.flashLoanSimple(
            address(this), // receiver (this contract)
            asset,         // asset to borrow
            amount,        // amount to borrow
            params,        // forwarded to executeOperation
            0              // referral code (none)
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Aave V3 Flash Loan Callback
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Aave V3 calls this function after transferring `amount` of `asset` to this contract
     * @dev    MUST approve POOL to pull back (amount + premium) before returning.
     *         Reverts if profit < minProfit to protect against sandwich attacks.
     *
     * @param asset     The borrowed token address
     * @param amount    The borrowed amount
     * @param premium   The flash loan fee (Aave V3: 0.05% = amount * 5 / 10000)
     * @param initiator Who called flashLoanSimple — must be address(this)
     * @param params    ABI-encoded ArbParams
     * @return          True on success (reverts on failure)
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override nonReentrant returns (bool) {
        // ── Security checks ──────────────────────────────────────────────────
        if (msg.sender != address(POOL)) revert NotPool();
        if (initiator  != address(this)) revert NotInitiator();

        // ── Decode parameters ────────────────────────────────────────────────
        ArbParams memory arb = abi.decode(params, (ArbParams));

        // ── Record balance before arb ────────────────────────────────────────
        uint256 balanceBefore = IERC20(asset).balanceOf(address(this));

        // ── Execute arbitrage ────────────────────────────────────────────────
        // Leg 1: Buy tokenB with tokenA on the cheaper DEX
        uint256 tokenBReceived = _executeSwap(
            arb.buyDex,
            arb.tokenA,       // tokenIn  = borrowed asset
            arb.tokenB,       // tokenOut = target token
            arb.amountIn,     // sell the full borrowed amount
            1                 // minOut=1 here; real slippage guard is profit check below
        );

        require(tokenBReceived > 0, "FlashLoanArb: buy swap returned 0");

        // Leg 2: Sell tokenB back to tokenA on the more expensive DEX
        uint256 tokenAReceived = _executeSwap(
            arb.sellDex,
            arb.tokenB,       // tokenIn  = tokenB from leg 1
            arb.tokenA,       // tokenOut = borrowed asset (need to repay)
            tokenBReceived,   // sell everything received in leg 1
            1                 // minOut — profit check below is the real guard
        );

        require(tokenAReceived > 0, "FlashLoanArb: sell swap returned 0");

        // ── Calculate profit ─────────────────────────────────────────────────
        uint256 totalOwed  = amount + premium;  // borrowed + 0.05% fee
        uint256 balanceNow = IERC20(asset).balanceOf(address(this));

        // balanceBefore includes the flash-loaned funds (they were transferred in before callback)
        // balanceNow should be >= balanceBefore if the arb was profitable
        if (balanceNow < totalOwed) {
            // Not enough to repay — this will cause the tx to revert in Aave
            revert InsufficientProfit(0, totalOwed);
        }

        uint256 grossProfit = balanceNow - totalOwed;

        // ── Slippage / sandwich protection ──────────────────────────────────
        if (grossProfit < arb.minProfit) {
            emit ArbFailed(asset, amount, grossProfit, arb.minProfit, "below minProfit");
            revert InsufficientProfit(grossProfit, arb.minProfit);
        }

        // ── Approve Aave to pull repayment ───────────────────────────────────
        IERC20(asset).forceApprove(address(POOL), totalOwed);

        // ── Send profit to owner ──────────────────────────────────────────────
        if (grossProfit > 0) {
            IERC20(asset).safeTransfer(owner(), grossProfit);
        }

        // ── Update stats ──────────────────────────────────────────────────────
        totalProfit[asset] += grossProfit;
        totalExecutions++;

        emit ArbExecuted(
            asset,
            amount,
            premium,
            grossProfit,
            arb.buyDex.router,
            arb.sellDex.router
        );

        return true;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // View helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Simulate an arb to check profitability before executing
     * @param asset   Token to borrow
     * @param amount  Borrow amount
     * @param params  ABI-encoded ArbParams
     * @return netProfit  Expected profit after repaying flash loan premium
     *                    Negative = losing trade
     */
    function simulateArb(
        address asset,
        uint256 amount,
        bytes calldata params
    ) external view returns (int256 netProfit) {
        ArbParams memory arb = abi.decode(params, (ArbParams));
        uint256 premium = (amount * POOL.FLASHLOAN_PREMIUM_TOTAL()) / 10_000;

        // Quote leg 1
        uint256 tokenBOut;
        if (arb.buyDex.dexType == DexType.UNISWAP_V2 || arb.buyDex.dexType == DexType.SUSHISWAP) {
            tokenBOut = getV2Quote(arb.buyDex.router, arb.tokenA, arb.tokenB, arb.amountIn);
        } else {
            tokenBOut = getV2SpotPrice(arb.buyDex.factory, arb.tokenA, arb.tokenB, arb.amountIn);
        }

        if (tokenBOut == 0) return type(int256).min;

        // Quote leg 2
        uint256 tokenAOut;
        if (arb.sellDex.dexType == DexType.UNISWAP_V2 || arb.sellDex.dexType == DexType.SUSHISWAP) {
            tokenAOut = getV2Quote(arb.sellDex.router, arb.tokenB, arb.tokenA, tokenBOut);
        } else {
            tokenAOut = getV2SpotPrice(arb.sellDex.factory, arb.tokenB, arb.tokenA, tokenBOut);
        }

        // Net = received - borrowed - premium
        netProfit = int256(tokenAOut) - int256(amount) - int256(premium);
    }

    /**
     * @notice Returns flash loan premium for a given amount
     * @param amount  Flash loan amount
     * @return premium Amount owed on top of principal
     */
    function getFlashLoanPremium(uint256 amount) external view returns (uint256 premium) {
        premium = (amount * POOL.FLASHLOAN_PREMIUM_TOTAL()) / 10_000;
    }

    /**
     * @notice Returns token balance held by this contract
     */
    function getTokenBalance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Admin functions
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @notice Emergency: withdraw any ERC-20 token stuck in the contract
     * @param token   Token address (address(0) for ETH)
     * @param amount  Amount to withdraw (0 = entire balance)
     * @param to      Recipient address
     */
    function emergencyWithdraw(
        address token,
        uint256 amount,
        address to
    ) external onlyOwner nonReentrant {
        if (to == address(0)) revert ZeroAddress();

        if (token == address(0)) {
            // ETH withdrawal
            uint256 ethBalance = address(this).balance;
            uint256 withdrawAmount = amount == 0 ? ethBalance : amount;
            require(withdrawAmount <= ethBalance, "FlashLoanArb: insufficient ETH");

            (bool success, ) = payable(to).call{value: withdrawAmount}("");
            require(success, "FlashLoanArb: ETH transfer failed");
            emit EthWithdrawn(withdrawAmount, to);
        } else {
            // ERC-20 withdrawal
            uint256 tokenBalance = IERC20(token).balanceOf(address(this));
            uint256 withdrawAmount = amount == 0 ? tokenBalance : amount;
            require(withdrawAmount <= tokenBalance, "FlashLoanArb: insufficient token balance");

            IERC20(token).safeTransfer(to, withdrawAmount);
            emit TokensWithdrawn(token, withdrawAmount, to);
        }
    }

    /**
     * @notice Pause / unpause the contract (emergency stop)
     */
    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit PausedStateChanged(_paused);
    }

    /**
     * @notice Pre-approve a token for a router (saves gas on first trade)
     * @param token   Token to approve
     * @param spender Router / spender address
     */
    function approveToken(address token, address spender, uint256 amount) external onlyOwner {
        IERC20(token).forceApprove(spender, amount);
    }

    /**
     * @notice Batch approve multiple tokens at once
     */
    function batchApprove(
        address[] calldata tokens,
        address[] calldata spenders,
        uint256[] calldata amounts
    ) external onlyOwner {
        require(
            tokens.length == spenders.length && tokens.length == amounts.length,
            "FlashLoanArb: array length mismatch"
        );
        for (uint256 i = 0; i < tokens.length; i++) {
            IERC20(tokens[i]).forceApprove(spenders[i], amounts[i]);
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Receive ETH (for gas funding)
    // ─────────────────────────────────────────────────────────────────────────

    receive() external payable {}

    fallback() external payable {}
}
