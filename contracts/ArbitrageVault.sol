// SPDX-License-Identifier: MIT
pragma solidity ^0.8.27;

import "./TradingBot.sol";

interface IArbToken {
    function balanceOf(address account) external view returns (uint256);
    function allowance(address account, address spender) external view returns (uint256);
}

interface IArbPool {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function fee() external view returns (uint24);
    function factory() external view returns (address);
}

interface IArbFactory {
    function getPool(address a, address b, uint24 fee) external view returns (address);
}

interface IArbRouterFactory {
    function factory() external view returns (address);
}

/// Dedicated f564-funded two-pool executor. There is no operator single-swap escape hatch.
/// Ranges and prices are not fixed here: each cycle supplies two pool-native sqrt price limits.
/// A partial fill, stranded new fETH, or insufficient f564 profit reverts BOTH swaps.
/// Profit excludes the operator's native ETH gas bill. Tokens must be standard, non-rebasing ERC20s.
contract ArbitrageVault {
    ISwapRouter02 public immutable router;
    address public immutable quoteToken;
    address public immutable baseToken;
    address public immutable pool0;
    address public immutable pool1;
    uint24 public immutable fee0;
    uint24 public immutable fee1;
    address public owner;
    address public pendingOwner;
    address public operator;
    bool public paused = true;
    uint256 public maxQuoteIn;
    uint256 public maxBaseIn;
    uint256 public minProfitFloor;
    bool private entered;

    struct Cycle {
        bool buyPool0;
        uint256 amountIn;
        uint256 minBaseOut;
        uint256 minQuoteOut;
        uint256 minProfit;
        uint256 deadline;
        uint160 buySqrtPriceLimitX96;
        uint160 sellSqrtPriceLimitX96;
    }

    event Arbitrage(bool indexed buyPool0, uint256 quoteIn, uint256 baseTraded, uint256 quoteOut, uint256 profit);
    event Withdraw(address indexed token, uint256 amount);
    event LimitsSet(uint256 maxQuoteIn, uint256 maxBaseIn, uint256 minProfitFloor);
    event Paused(bool paused);
    event OperatorChanged(address indexed operator);
    event OwnershipTransferStarted(address indexed owner, address indexed pendingOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier nonReentrant() {
        require(!entered, "reentrant");
        entered = true;
        _;
        entered = false;
    }

    constructor(
        address owner_, address operator_, address router_, address quote_, address base_,
        address[2] memory pools, uint256[3] memory limits
    ) {
        require(owner_ != address(0) && owner_ != address(this), "bad owner");
        require(router_.code.length > 0, "router not a contract");
        require(quote_ != base_ && quote_.code.length > 0 && base_.code.length > 0, "bad tokens");
        require(pools[0] != pools[1], "same pool");
        address factory = IArbRouterFactory(router_).factory();
        uint24 firstFee = _checkPool(pools[0], quote_, base_, factory);
        uint24 secondFee = _checkPool(pools[1], quote_, base_, factory);
        require(firstFee != secondFee, "same fee");
        router = ISwapRouter02(router_);
        quoteToken = quote_;
        baseToken = base_;
        pool0 = pools[0];
        pool1 = pools[1];
        fee0 = firstFee;
        fee1 = secondFee;
        owner = owner_;
        operator = operator_;
        _limits(limits[0], limits[1], limits[2]);
        emit OwnershipTransferred(address(0), owner_);
        emit OperatorChanged(operator_);
        emit Paused(true);
    }

    receive() external payable {}

    function execute(Cycle calldata c) external nonReentrant returns (uint256 profit) {
        require(msg.sender == operator || msg.sender == owner, "not operator");
        require(!paused, "paused");
        require(block.timestamp <= c.deadline, "expired");
        require(c.amountIn > 0 && c.amountIn <= maxQuoteIn, "quote cap");
        require(c.minBaseOut > 0 && c.minQuoteOut > 0, "zero minimum");
        require(c.minProfit >= minProfitFloor, "profit floor");
        _checkPriceLimit(c.buySqrtPriceLimitX96);
        _checkPriceLimit(c.sellSqrtPriceLimitX96);

        uint256 quoteBefore = IArbToken(quoteToken).balanceOf(address(this));
        uint256 baseBefore = IArbToken(baseToken).balanceOf(address(this));
        uint256 baseBought = _swap(quoteToken, baseToken, c.buyPool0 ? fee0 : fee1,
            c.amountIn, c.minBaseOut, c.buySqrtPriceLimitX96);
        require(baseBought <= maxBaseIn, "base cap");
        uint256 requiredOut = c.amountIn + c.minProfit;
        uint256 quoteOut = _swap(baseToken, quoteToken, c.buyPool0 ? fee1 : fee0,
            baseBought, c.minQuoteOut > requiredOut ? c.minQuoteOut : requiredOut,
            c.sellSqrtPriceLimitX96);
        require(IArbToken(baseToken).balanceOf(address(this)) == baseBefore, "base inventory changed");
        uint256 quoteAfter = IArbToken(quoteToken).balanceOf(address(this));
        require(quoteAfter >= quoteBefore + c.minProfit, "insufficient profit");
        profit = quoteAfter - quoteBefore;
        emit Arbitrage(c.buyPool0, c.amountIn, baseBought, quoteOut, profit);
    }

    function _swap(address tokenIn, address tokenOut, uint24 fee, uint256 amountIn,
        uint256 minimum, uint160 limit) internal returns (uint256 received)
    {
        uint256 beforeIn = IArbToken(tokenIn).balanceOf(address(this));
        uint256 beforeOut = IArbToken(tokenOut).balanceOf(address(this));
        require(amountIn > 0 && beforeIn >= amountIn, "insufficient balance");
        // Strict reset, including nonzero-to-nonzero approval tokens; no residual spending power.
        _call(tokenIn, abi.encodeCall(IERC20.approve, (address(router), 0)));
        _call(tokenIn, abi.encodeCall(IERC20.approve, (address(router), amountIn)));
        uint256 reported = router.exactInputSingle(ISwapRouter02.ExactInputSingleParams({
            tokenIn: tokenIn, tokenOut: tokenOut, fee: fee, recipient: address(this),
            amountIn: amountIn, amountOutMinimum: minimum, sqrtPriceLimitX96: limit
        }));
        _call(tokenIn, abi.encodeCall(IERC20.approve, (address(router), 0)));
        require(IArbToken(tokenIn).allowance(address(this), address(router)) == 0, "allowance remains");
        require(IArbToken(tokenIn).balanceOf(address(this)) == beforeIn - amountIn, "partial input");
        received = IArbToken(tokenOut).balanceOf(address(this)) - beforeOut;
        require(received >= minimum && received == reported, "bad output");
    }

    function _checkPriceLimit(uint160 limit) internal pure {
        require(limit > 4295128739 && limit < 1461446703485210103287273052203988822378723970342,
            "bad price limit");
    }

    function _checkPool(address pool, address quote, address base, address factory) private view returns (uint24 fee) {
        require(pool.code.length > 0, "pool not a contract");
        address t0 = IArbPool(pool).token0();
        address t1 = IArbPool(pool).token1();
        require((t0 == quote && t1 == base) || (t0 == base && t1 == quote), "pool tokens");
        require(IArbPool(pool).factory() == factory, "pool factory");
        fee = IArbPool(pool).fee();
        require(IArbFactory(factory).getPool(quote, base, fee) == pool, "unregistered pool");
    }

    function setLimits(uint256 quoteCap, uint256 baseCap, uint256 profitFloor) external onlyOwner nonReentrant {
        _limits(quoteCap, baseCap, profitFloor);
    }

    function _limits(uint256 quoteCap, uint256 baseCap, uint256 profitFloor) private {
        require(quoteCap > 0 && baseCap > 0 && profitFloor > 0, "zero limit");
        maxQuoteIn = quoteCap;
        maxBaseIn = baseCap;
        minProfitFloor = profitFloor;
        emit LimitsSet(quoteCap, baseCap, profitFloor);
    }

    function setPaused(bool value) external onlyOwner nonReentrant {
        paused = value;
        emit Paused(value);
    }

    function setOperator(address value) external onlyOwner nonReentrant {
        operator = value;
        emit OperatorChanged(value);
    }

    function withdraw(address token, uint256 amount) external onlyOwner nonReentrant {
        _call(token, abi.encodeCall(IERC20.transfer, (owner, amount)));
        emit Withdraw(token, amount);
    }

    function withdrawETH(uint256 amount) external onlyOwner nonReentrant {
        (bool ok,) = owner.call{value: amount}("");
        require(ok, "eth transfer failed");
        emit Withdraw(address(0), amount);
    }

    function transferOwnership(address value) external onlyOwner nonReentrant {
        require(value != address(0) && value != address(this), "bad owner");
        pendingOwner = value;
        emit OwnershipTransferStarted(owner, value);
    }

    function acceptOwnership() external nonReentrant {
        require(msg.sender == pendingOwner, "not pending owner");
        emit OwnershipTransferred(owner, msg.sender);
        owner = msg.sender;
        pendingOwner = address(0);
    }

    function _call(address token, bytes memory data) private {
        require(token.code.length > 0, "not a contract");
        (bool ok, bytes memory ret) = token.call(data);
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "token call failed");
    }
}
