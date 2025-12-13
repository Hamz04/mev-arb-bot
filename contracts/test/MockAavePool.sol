// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

interface IFlashLoanCb {
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

interface IMintableMock {
    function mint(address to, uint256 amount) external;
}

/**
 * @title MockAavePool
 * @notice Simulates Aave V3 Pool flash loan for unit testing.
 *         Mints borrowed tokens — no real liquidity needed.
 *         Premium: 5 bps (0.05%), matching real Aave V3.
 */
contract MockAavePool {
    using SafeERC20 for IERC20;

    uint128 public constant FLASHLOAN_PREMIUM_TOTAL = 5;
    uint128 public constant FLASHLOAN_PREMIUM_TO_PROTOCOL = 4;

    function ADDRESSES_PROVIDER() external pure returns (address) {
        return address(1);
    }

    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16
    ) external {
        uint256 premium = (amount * FLASHLOAN_PREMIUM_TOTAL) / 10_000;

        // Mint borrowed amount to receiver (simulates Aave transferring funds)
        IMintableMock(asset).mint(receiverAddress, amount);

        // Call the receiver's executeOperation callback
        // initiator must equal receiverAddress to pass FlashLoanArb's security check
        bool success = IFlashLoanCb(receiverAddress).executeOperation(
            asset,
            amount,
            premium,
            receiverAddress,
            params
        );
        require(success, "MockAavePool: executeOperation returned false");

        // Pull back amount + premium from receiver
        IERC20(asset).safeTransferFrom(receiverAddress, address(this), amount + premium);
    }
}
