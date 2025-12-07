// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IFlashLoanSimpleReceiver
 * @notice Interface for Aave V3 flash loan simple receiver
 * @dev Implement this interface to receive flash loans from Aave V3 Pool
 *      The executeOperation function must approve the Pool to pull the
 *      borrowed amount + premium before returning true.
 */
interface IFlashLoanSimpleReceiver {
    /**
     * @notice Executes an operation after receiving the flash-borrowed asset
     * @dev Ensure that the contract can return the debt + premium, e.g., has
     *      enough funds to repay and has approved the Pool to pull the total amount
     * @param asset The address of the flash-borrowed asset
     * @param amount The amount of the flash-borrowed asset
     * @param premium The fee of the flash-borrowed asset — Aave V3 charges 0.05% (5 bps)
     * @param initiator The address of the flashLoan initiator
     * @param params The byte-encoded params passed when initiating the flash loan
     * @return True if the execution of the operation succeeds, false otherwise
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}

/**
 * @title IFlashLoanReceiver (multi-asset version)
 * @notice Interface for Aave V3 multi-asset flash loan receiver
 */
interface IFlashLoanReceiver {
    /**
     * @notice Executes an operation after receiving the flash-borrowed assets
     * @param assets The addresses of the flash-borrowed assets
     * @param amounts The amounts of the flash-borrowed assets
     * @param premiums The fee of each flash-borrowed asset
     * @param initiator The address of the flashLoan initiator
     * @param params The byte-encoded params passed when initiating the flash loan
     * @return True if the execution of the operation succeeds, false otherwise
     */
    function executeOperation(
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata premiums,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}
