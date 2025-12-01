// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title IAavePool
 * @notice Interface for Aave V3 Pool — only the methods used by the arb bot
 * @dev Full interface: https://github.com/aave/aave-v3-core/blob/master/contracts/interfaces/IPool.sol
 */
interface IAavePool {
    /**
     * @notice Allows smart contracts to access the liquidity of the pool within one transaction,
     *         as long as the amount taken plus a fee is returned.
     * @dev IMPORTANT: The caller of this function receives a callback to IFlashLoanSimpleReceiver.executeOperation()
     * @param receiverAddress The address of the contract receiving the funds, implementing IFlashLoanSimpleReceiver
     * @param asset The address of the asset being flash-borrowed
     * @param amount The amount of the asset being flash-borrowed
     * @param params Variadic packed params to pass to the receiver as extra information
     * @param referralCode The code used to register the integrator originating the operation (0 if none)
     */
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    /**
     * @notice Allows smart contracts to access the liquidity of the pool within one transaction
     *         for multiple assets simultaneously.
     * @param receiverAddress The address of the contract receiving the funds
     * @param assets The addresses of the assets being flash-borrowed
     * @param amounts The amounts of the assets being flash-borrowed
     * @param interestRateModes Types of the debt to open if the flash loan is not returned
     *   0 -> Don't open any debt, just revert if funds can't be transferred from the receiver
     *   1 -> stable
     *   2 -> variable
     * @param onBehalfOf The address that will receive the debt in case of non-repayment
     * @param params Variadic packed params to pass to the receiver as extra information
     * @param referralCode The code used to register the integrator originating the operation
     */
    function flashLoan(
        address receiverAddress,
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata interestRateModes,
        address onBehalfOf,
        bytes calldata params,
        uint16 referralCode
    ) external;

    /**
     * @notice Returns the normalized income of the reserve
     * @param asset The address of the underlying asset of the reserve
     * @return The reserve's normalized income
     */
    function getReserveNormalizedIncome(address asset) external view returns (uint256);

    /**
     * @notice Returns the state and configuration of the reserve
     * @param asset The address of the underlying asset of the reserve
     * @return The state and configuration data of the reserve
     */
    function getReserveData(address asset) external view returns (ReserveData memory);

    /**
     * @notice Returns the addresses provider of the pool
     */
    function ADDRESSES_PROVIDER() external view returns (address);

    /**
     * @notice Returns the flashloan premium total (e.g. 5 = 0.05%)
     */
    function FLASHLOAN_PREMIUM_TOTAL() external view returns (uint128);

    /**
     * @notice Returns the flashloan premium to protocol (e.g. 4 = 0.04%)
     */
    function FLASHLOAN_PREMIUM_TO_PROTOCOL() external view returns (uint128);
}

/**
 * @notice Reserve data struct returned by getReserveData
 * @dev Matches Aave V3 DataTypes.ReserveData
 */
struct ReserveData {
    // Stores the reserve configuration
    ReserveConfigurationMap configuration;
    // The liquidity index. Expressed in ray
    uint128 liquidityIndex;
    // The current supply rate. Expressed in ray
    uint128 currentLiquidityRate;
    // Variable borrow index. Expressed in ray
    uint128 variableBorrowIndex;
    // The current variable borrow rate. Expressed in ray
    uint128 currentVariableBorrowRate;
    // The current stable borrow rate. Expressed in ray
    uint128 currentStableBorrowRate;
    // Timestamp of last update
    uint40 lastUpdateTimestamp;
    // The id of the reserve. Represents the position in the list of the active reserves
    uint16 id;
    // aToken address
    address aTokenAddress;
    // stableDebtToken address
    address stableDebtTokenAddress;
    // variableDebtToken address
    address variableDebtTokenAddress;
    // address of the interest rate strategy
    address interestRateStrategyAddress;
    // the current treasury balance, scaled
    uint128 accruedToTreasury;
    // the outstanding unbacked aTokens minted through the bridging feature
    uint128 unbacked;
    // the outstanding debt borrowed against this asset in isolation mode
    uint128 isolationModeTotalDebt;
}

struct ReserveConfigurationMap {
    uint256 data;
}

/**
 * @title IPoolAddressesProvider
 * @notice Defines the basic interface for a Pool Addresses Provider.
 */
interface IPoolAddressesProvider {
    function getPool() external view returns (address);
    function getPriceOracle() external view returns (address);
    function getACLManager() external view returns (address);
    function getACLAdmin() external view returns (address);
}
