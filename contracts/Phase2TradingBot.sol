// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import "./ArbitrageVault.sol";

/// One deployed contract, one balance: bounded single swaps and atomic cross-pool cycles.
/// Inheritance includes the cycle/owner logic in this contract's bytecode; no second vault is called.
contract Phase2TradingBot is ArbitrageVault {
    struct SingleSwap {
        bool buyBase;
        uint24 fee;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint256 deadline;
        uint160 sqrtPriceLimitX96;
    }

    event Swap(address indexed tokenIn, address indexed tokenOut, uint24 fee, uint256 amountIn, uint256 amountOut);

    constructor(
        address owner_, address operator_, address router_, address quote_, address base_,
        address[2] memory pools, uint256[3] memory limits
    ) ArbitrageVault(owner_, operator_, router_, quote_, base_, pools, limits) {
        require(operator_ != owner_, "separate operator required");
    }

    function contractVersion() external pure returns (uint256) { return 2; }

    function feeAllowed(uint24 fee) public view returns (bool) {
        return fee == fee0 || fee == fee1;
    }

    function tokenCap(address token) external view returns (uint256) {
        if (token == quoteToken) return maxQuoteIn;
        if (token == baseToken) return maxBaseIn;
        return 0;
    }

    /// The profit floor applies to execute(Cycle), not to a single entry or exit.
    /// The service owns inventory accounting across transactions; output always stays here.
    function swap(SingleSwap calldata s) external nonReentrant returns (uint256 amountOut) {
        require(msg.sender == operator || msg.sender == owner, "not operator");
        require(!paused, "paused");
        require(block.timestamp <= s.deadline, "expired");
        require(feeAllowed(s.fee), "fee not allowed");
        require(s.amountIn > 0 && s.amountIn <= (s.buyBase ? maxQuoteIn : maxBaseIn), "input cap");
        require(s.amountOutMinimum > 0, "zero minimum");
        _checkPriceLimit(s.sqrtPriceLimitX96);
        address tokenIn = s.buyBase ? quoteToken : baseToken;
        address tokenOut = s.buyBase ? baseToken : quoteToken;
        amountOut = _swap(tokenIn, tokenOut, s.fee, s.amountIn, s.amountOutMinimum, s.sqrtPriceLimitX96);
        // A favorable price must not create a position larger than a permitted whole-position exit.
        if (s.buyBase) require(amountOut <= maxBaseIn, "base cap");
        emit Swap(tokenIn, tokenOut, s.fee, s.amountIn, amountOut);
    }
}
