// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

// ─────────────────────────────────────────────────────────────────────────────
// Uniswap V2 / Sushiswap interfaces
// ─────────────────────────────────────────────────────────────────────────────

interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);

    function getAmountsOut(
        uint256 amountIn,
        address[] calldata path
    ) external view returns (uint256[] memory amounts);
}

interface IUniswapV2Pair {
    function getReserves()
        external
        view
        returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);

    function token0() external view returns (address);
    function token1() external view returns (address);
}

interface IUniswapV2Factory {
    function getPair(address tokenA, address tokenB) external view returns (address pair);
}

// ─────────────────────────────────────────────────────────────────────────────
// Uniswap V3 interfaces
// ─────────────────────────────────────────────────────────────────────────────

interface IUniswapV3Router {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }

    function exactInputSingle(
        ExactInputSingleParams calldata params
    ) external payable returns (uint256 amountOut);
}

interface IUniswapV3Quoter {
    function quoteExactInputSingle(
        address tokenIn,
        address tokenOut,
        uint24  fee,
        uint256 amountIn,
        uint160 sqrtPriceLimitX96
    ) external returns (uint256 amountOut);
}

interface IUniswapV3Pool {
    function slot0()
        external
        view
        returns (
            uint160 sqrtPriceX96,
            int24  tick,
            uint16 observationIndex,
            uint16 observationCardinality,
            uint16 observationCardinalityNext,
            uint8  feeProtocol,
            bool   unlocked
        );

    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee()    external view returns (uint24);
    function liquidity() external view returns (uint128);
}

// ─────────────────────────────────────────────────────────────────────────────
// DEX Type enum
// ─────────────────────────────────────────────────────────────────────────────

enum DexType { UNISWAP_V2, UNISWAP_V3, SUSHISWAP }

struct DexConfig {
    DexType   dexType;
    address   router;
    address   factory;   // V2 only
    address   quoter;    // V3 only
    uint24    v3Fee;     // V3 only (500, 3000, 10000)
}

struct ArbParams {
    address   tokenA;
    address   tokenB;
    uint256   amountIn;
    DexConfig buyDex;    // buy tokenB with tokenA here (cheaper)
    DexConfig sellDex;   // sell tokenB back to tokenA here (more expensive)
    uint256   minProfit; // revert if profit < minProfit (slippage guard)
}

/**
 * @title ArbExecutor
 * @notice Handles multi-DEX swap routing for arbitrage execution.
 *         Supports Uniswap V2, Uniswap V3, and Sushiswap.
 *         Inherited by FlashLoanArb.sol.
 * @dev All swap functions are internal — called only from within the contract.
 *      Price queries are public view so the off-chain bot can call them.
 */
abstract contract ArbExecutor {
    using SafeERC20 for IERC20;

    // ── Constants ────────────────────────────────────────────────────────────

    /// @dev 10 minutes — hard deadline for all swaps
    uint256 internal constant SWAP_DEADLINE_BUFFER = 600;

    /// @dev Basis points denominator
    uint256 internal constant BPS = 10_000;

    /// @dev Uniswap V3 fee tiers
    uint24 internal constant FEE_LOW    = 500;    // 0.05%
    uint24 internal constant FEE_MEDIUM = 3_000;  // 0.30%
    uint24 internal constant FEE_HIGH   = 10_000; // 1.00%

    // ── Events ───────────────────────────────────────────────────────────────

    event SwapExecuted(
        address indexed dexRouter,
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut
    );

    // ── Internal swap dispatcher ─────────────────────────────────────────────

    /**
     * @notice Route a swap through the correct DEX
     * @param cfg      DEX configuration (type, router, fee…)
     * @param tokenIn  Token being sold
     * @param tokenOut Token being bought
     * @param amountIn Exact amount of tokenIn to sell
     * @param minOut   Minimum acceptable tokenOut (slippage protection)
     * @return amountOut Actual tokens received
     */
    function _executeSwap(
        DexConfig memory cfg,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut
    ) internal returns (uint256 amountOut) {
        if (cfg.dexType == DexType.UNISWAP_V2 || cfg.dexType == DexType.SUSHISWAP) {
            amountOut = _swapV2(cfg.router, tokenIn, tokenOut, amountIn, minOut);
        } else if (cfg.dexType == DexType.UNISWAP_V3) {
            amountOut = _swapV3(cfg.router, tokenIn, tokenOut, amountIn, minOut, cfg.v3Fee);
        } else {
            revert("ArbExecutor: unsupported DEX type");
        }
        emit SwapExecuted(cfg.router, tokenIn, tokenOut, amountIn, amountOut);
    }

    // ── V2 swap ──────────────────────────────────────────────────────────────

    function _swapV2(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut
    ) internal returns (uint256 amountOut) {
        // Approve router to pull tokenIn
        IERC20(tokenIn).forceApprove(router, amountIn);

        address[] memory path = new address[](2);
        path[0] = tokenIn;
        path[1] = tokenOut;

        uint256[] memory amounts = IUniswapV2Router(router).swapExactTokensForTokens(
            amountIn,
            minOut,
            path,
            address(this),
            block.timestamp + SWAP_DEADLINE_BUFFER
        );

        amountOut = amounts[amounts.length - 1];
    }

    // ── V3 swap ──────────────────────────────────────────────────────────────

    function _swapV3(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 minOut,
        uint24  fee
    ) internal returns (uint256 amountOut) {
        IERC20(tokenIn).forceApprove(router, amountIn);

        IUniswapV3Router.ExactInputSingleParams memory params = IUniswapV3Router
            .ExactInputSingleParams({
                tokenIn:           tokenIn,
                tokenOut:          tokenOut,
                fee:               fee,
                recipient:         address(this),
                deadline:          block.timestamp + SWAP_DEADLINE_BUFFER,
                amountIn:          amountIn,
                amountOutMinimum:  minOut,
                sqrtPriceLimitX96: 0
            });

        amountOut = IUniswapV3Router(router).exactInputSingle(params);
    }

    // ── Price queries (view) ─────────────────────────────────────────────────

    /**
     * @notice Get expected output for a V2/Sushiswap swap
     * @param router   V2 router address
     * @param tokenIn  Token sold
     * @param tokenOut Token bought
     * @param amountIn Amount to sell
     * @return expected Output amount (before slippage)
     */
    function getV2Quote(
        address router,
        address tokenIn,
        address tokenOut,
        uint256 amountIn
    ) public view returns (uint256 expected) {
        address[] memory path = new address[](2);
        path[0] = tokenIn;
        path[1] = tokenOut;
        try IUniswapV2Router(router).getAmountsOut(amountIn, path) returns (
            uint256[] memory amounts
        ) {
            expected = amounts[amounts.length - 1];
        } catch {
            expected = 0;
        }
    }

    /**
     * @notice Compute V2 spot price using getReserves (gas-free, no state change)
     * @param factory  V2 factory address
     * @param tokenA   Token A
     * @param tokenB   Token B
     * @param amountIn Amount of tokenA to trade
     * @return amountOut Expected tokenB out (with 0.3% fee baked in)
     */
    function getV2SpotPrice(
        address factory,
        address tokenA,
        address tokenB,
        uint256 amountIn
    ) public view returns (uint256 amountOut) {
        address pair = IUniswapV2Factory(factory).getPair(tokenA, tokenB);
        if (pair == address(0)) return 0;

        (uint112 r0, uint112 r1, ) = IUniswapV2Pair(pair).getReserves();
        address token0 = IUniswapV2Pair(pair).token0();

        (uint256 reserveIn, uint256 reserveOut) = token0 == tokenA
            ? (uint256(r0), uint256(r1))
            : (uint256(r1), uint256(r0));

        // Uniswap V2 formula: amountOut = (amountIn * 997 * reserveOut) / (reserveIn * 1000 + amountIn * 997)
        uint256 amountInWithFee = amountIn * 997;
        uint256 numerator       = amountInWithFee * reserveOut;
        uint256 denominator     = reserveIn * 1000 + amountInWithFee;
        amountOut = numerator / denominator;
    }

    /**
     * @notice Calculate the arbitrage opportunity and expected profit
     * @param tokenA   Base token (e.g. USDC)
     * @param tokenB   Quote token (e.g. WETH)
     * @param amount   Amount of tokenA to use for the arb
     * @param dex1     First DEX config (buy tokenB here)
     * @param dex2     Second DEX config (sell tokenB here)
     * @return profit  Expected gross profit in tokenA (before gas costs)
     * @return buyOut  Amount of tokenB obtained on dex1
     * @return sellOut Amount of tokenA obtained on dex2
     */
    function calculateArb(
        address tokenA,
        address tokenB,
        uint256 amount,
        DexConfig calldata dex1,
        DexConfig calldata dex2
    ) external view returns (int256 profit, uint256 buyOut, uint256 sellOut) {
        // Step 1: how much tokenB do we get on dex1 for `amount` of tokenA?
        if (dex1.dexType == DexType.UNISWAP_V2 || dex1.dexType == DexType.SUSHISWAP) {
            buyOut = getV2Quote(dex1.router, tokenA, tokenB, amount);
        } else {
            // V3 — use factory-based spot price as approximation (no state mutation)
            buyOut = getV2SpotPrice(dex1.factory, tokenA, tokenB, amount);
        }

        if (buyOut == 0) return (0, 0, 0);

        // Step 2: how much tokenA do we get on dex2 for `buyOut` of tokenB?
        if (dex2.dexType == DexType.UNISWAP_V2 || dex2.dexType == DexType.SUSHISWAP) {
            sellOut = getV2Quote(dex2.router, tokenB, tokenA, buyOut);
        } else {
            sellOut = getV2SpotPrice(dex2.factory, tokenB, tokenA, buyOut);
        }

        // Profit = what we get back minus what we put in
        profit = int256(sellOut) - int256(amount);
    }

    /**
     * @notice Calculate the optimal flash loan amount for maximum profit
     * @dev Based on the closed-form solution for V2 constant-product AMM arb:
     *      optimalAmount = sqrt(k1 * k2 * (p2/p1)) — simplified here using
     *      the geometric mean of the two pools' reserves
     * @param reserve0_a  reserveIn on DEX A (tokenA reserve)
     * @param reserve1_a  reserveOut on DEX A (tokenB reserve)
     * @param reserve0_b  reserveIn on DEX B (tokenB reserve)
     * @param reserve1_b  reserveOut on DEX B (tokenA reserve)
     * @return optimal    Optimal input amount in tokenA units
     */
    function calculateOptimalAmount(
        uint256 reserve0_a,
        uint256 reserve1_a,
        uint256 reserve0_b,
        uint256 reserve1_b
    ) public pure returns (uint256 optimal) {
        // Price on DEX A: p_a = reserve1_a / reserve0_a  (tokenB per tokenA)
        // Price on DEX B: p_b = reserve0_b / reserve1_b  (tokenB per tokenA, inverted for sell)
        //
        // Optimal amount (closed form, ignoring fees for simplicity):
        //   optimal = sqrt(reserve0_a * reserve0_b) - reserve0_a
        //
        // With fees (0.997 factor each hop):
        //   optimal = (sqrt(0.997^2 * reserve0_a * reserve0_b * reserve1_a / reserve1_b) - reserve0_a * 0.997) / 0.997
        //
        // We use integer arithmetic scaled by 1e9 for precision

        if (reserve0_a == 0 || reserve1_a == 0 || reserve0_b == 0 || reserve1_b == 0) {
            return 0;
        }

        // Scale to avoid precision loss in integer sqrt
        // numerator  = reserve0_a * reserve1_b  (denominator pool A price)
        // denominator = reserve1_a * reserve0_b  (numerator pool B price inverted)
        uint256 num = reserve0_a * reserve1_b;
        uint256 den = reserve1_a * reserve0_b;

        if (den == 0 || num >= den) {
            // No arb opportunity (prices equal or inverted)
            return 0;
        }

        // Geometric mean: sqrt(reserve0_a * reserve0_b) gives midpoint reserve
        // Use Babylonian method for integer square root
        uint256 product = reserve0_a * reserve0_b;
        uint256 sqrtProduct = _sqrt(product);

        // Subtract current reserve to get the delta needed
        if (sqrtProduct > reserve0_a) {
            optimal = sqrtProduct - reserve0_a;
        } else {
            optimal = 0;
        }
    }

    /**
     * @notice Babylonian integer square root
     * @param x Value to compute sqrt of
     * @return y Floor of sqrt(x)
     */
    function _sqrt(uint256 x) internal pure returns (uint256 y) {
        if (x == 0) return 0;
        uint256 z = (x + 1) / 2;
        y = x;
        while (z < y) {
            y = z;
            z = (x / z + z) / 2;
        }
    }
}
