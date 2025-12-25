// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

interface IMintable {
    function mint(address to, uint256 amount) external;
}

/**
 * @title MockV2Router
 * @notice Simulates a Uniswap V2 router for testing.
 *         outputMultiplier controls exchange rate:
 *         1000 = 1:1, 1020 = 1.02x output, 1050 = 1.05x output
 */
contract MockV2Router {
    using SafeERC20 for IERC20;

    uint256 public outputMultiplier;

    constructor(uint256 _multiplier) {
        outputMultiplier = _multiplier;
    }

    function setMultiplier(uint256 m) external {
        outputMultiplier = m;
    }

    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256
    ) external returns (uint256[] memory amounts) {
        require(path.length >= 2, "MockV2Router: bad path");
        IERC20(path[0]).safeTransferFrom(msg.sender, address(this), amountIn);
        uint256 amountOut = (amountIn * outputMultiplier) / 1000;
        require(amountOut >= amountOutMin, "MockV2Router: slippage exceeded");
        IMintable(path[path.length - 1]).mint(to, amountOut);
        amounts = new uint256[](path.length);
        amounts[0] = amountIn;
        amounts[amounts.length - 1] = amountOut;
    }

    function getAmountsOut(
        uint256 amountIn,
        address[] calldata path
    ) external view returns (uint256[] memory amounts) {
        amounts = new uint256[](path.length);
        amounts[0] = amountIn;
        amounts[amounts.length - 1] = (amountIn * outputMultiplier) / 1000;
    }
}
